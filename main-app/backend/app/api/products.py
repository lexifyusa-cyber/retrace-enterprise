"""
Product management API endpoints.
"""

from typing import Optional

from app.api.files import is_path_allowed
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from app.db.database import get_session
from app.models.product import Product
from app.models.folder_group import FolderGroup, FolderPath
from app.models.pod import Pod
from app.models.product_access import ProductAccess
from app.models.tenant import Tenant
from app.models.user import User
from app.core.security import get_optional_user, get_current_user, CurrentUser
from sqlalchemy import func

logger = structlog.get_logger()
router = APIRouter()


class FolderPathCreate(BaseModel):
    """Schema for creating a folder path."""
    absolute_path: str
    scan_recursive: bool = True
    file_filters: Optional[dict] = Field(default_factory=lambda: {"include": [], "exclude": []})


class FolderGroupCreate(BaseModel):
    """Schema for creating a folder group within a product."""
    pod_id: str
    group_name: str = Field(..., min_length=1, max_length=255)
    group_type: str = Field(
        default="code",
        pattern="^(code|documentation|tickets|other)$"
    )
    folder_paths: list[FolderPathCreate]


class ProductCreate(BaseModel):
    """Schema for creating a product."""
    product_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    auto_generate_description: bool = Field(default=True)
    folder_groups: list[FolderGroupCreate] = Field(default_factory=list)


class ProductUpdate(BaseModel):
    """Schema for updating a product."""
    product_name: Optional[str] = None
    description: Optional[str] = None


class QAPair(BaseModel):
    """A single question-answer pair from an agent conversation."""
    question: str
    answer: str


class SyncQARequest(BaseModel):
    """Request body for syncing agent Q&A pairs to training data."""
    qa_pairs: list[QAPair] = Field(..., min_length=1, max_length=10)


class FolderGroupResponse(BaseModel):
    """Schema for folder group response."""
    group_id: str
    product_id: str
    pod_id: str
    group_name: str
    group_type: str
    namespace: str
    created_at: Optional[str]
    last_trained: Optional[str]
    training_status: str
    folder_paths: list[dict]
    metadata: dict = {}


class ProductResponse(BaseModel):
    """Schema for product response."""
    product_id: str
    product_name: str
    description: Optional[str]
    auto_generate_description: bool = True
    created_at: Optional[str]
    folder_groups: list[FolderGroupResponse]
    metadata: dict = {}


@router.get("", response_model=list[ProductResponse])
async def list_products(
    session: AsyncSession = Depends(get_session),
    current_user: Optional[CurrentUser] = Depends(get_optional_user),
):
    """List products. Admins see all; others see only products they have access to."""
    query = select(Product).options(
        selectinload(Product.folder_groups).selectinload(FolderGroup.folder_paths)
    )

    if current_user and current_user.role not in ("admin", "super_admin", "tenant_admin", "zero_admin"):
        accessible = select(ProductAccess.product_id).where(ProductAccess.user_id == current_user.user_id)
        query = query.where(Product.product_id.in_(accessible))

    result = await session.execute(query)
    products = result.scalars().all()
    
    return [ProductResponse(**product.to_dict()) for product in products]


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    product_data: ProductCreate,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create a new product. Admins: unlimited. User admins: subject to tenant product limit."""
    if not current_user.has_role("user_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins and user admins can create products")

    # Enforce product limit for user_admin (not full admin)
    if current_user.role == "user_admin" and current_user.tenant_id:
        count_result = await session.execute(
            select(func.count(Product.product_id)).where(Product.created_by == current_user.user_id)
        )
        current_count = count_result.scalar() or 0
        user = await session.get(User, current_user.user_id)
        tenant = await session.get(Tenant, current_user.tenant_id)
        # Per-user limit overrides tenant default when set
        max_allowed = (
            user.max_products
            if user and user.max_products is not None
            else (tenant.max_products_per_user_admin if tenant else 10)
        )
        if current_count >= max_allowed:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Product limit reached ({max_allowed}). Contact your admin to increase the limit.",
            )

    product_id = str(uuid4())
    product = Product(
        product_id=product_id,
        tenant_id=current_user.tenant_id,
        created_by=current_user.user_id,
        product_name=product_data.product_name,
        description=product_data.description,
        auto_generate_description=product_data.auto_generate_description,
    )
    
    # Add product to session first so it's available for relationships
    session.add(product)
    
    # Add folder groups if provided
    for group_data in product_data.folder_groups:
        # Verify POD exists
        result = await session.execute(
            select(Pod).where(Pod.pod_id == group_data.pod_id)
        )
        pod = result.scalar_one_or_none()
        
        if not pod:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"POD {group_data.pod_id} not found"
            )
        
        # For __local__ pod, validate paths are under allowed browse roots
        if group_data.pod_id == "__local__":
            for path_data in group_data.folder_paths:
                if not is_path_allowed(path_data.absolute_path):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Path is not under allowed browse roots: {path_data.absolute_path}",
                    )
        
        # Generate namespace
        namespace = f"product-{product_id[:8]}-{group_data.group_name.lower().replace(' ', '-')}"
        
        # Check namespace uniqueness
        existing = await session.execute(
            select(FolderGroup).where(FolderGroup.namespace == namespace)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Namespace {namespace} already exists"
            )
        
        # Create folder group
        group_id = str(uuid4())
        group = FolderGroup(
            group_id=group_id,
            product_id=product_id,
            pod_id=group_data.pod_id,
            group_name=group_data.group_name,
            group_type=group_data.group_type,
            namespace=namespace,
        )
        
        # Add folder paths
        for path_data in group_data.folder_paths:
            folder_path = FolderPath(
                path_id=str(uuid4()),
                group_id=group_id,
                absolute_path=path_data.absolute_path,
                scan_recursive=path_data.scan_recursive,
                file_filters=path_data.file_filters,
            )
            group.folder_paths.append(folder_path)
        
        # Add group to session - relationship maintained via product_id foreign key
        session.add(group)
    
    await session.commit()
    
    # Reload with relationships
    result = await session.execute(
        select(Product)
        .options(
            selectinload(Product.folder_groups).selectinload(FolderGroup.folder_paths)
        )
        .where(Product.product_id == product_id)
    )
    product = result.scalar_one()
    
    logger.info(
        "Product created",
        product_id=product_id,
        product_name=product_data.product_name
    )
    
    return ProductResponse(**product.to_dict())


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Get a specific product by ID."""
    result = await session.execute(
        select(Product)
        .options(
            selectinload(Product.folder_groups).selectinload(FolderGroup.folder_paths)
        )
        .where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()
    
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )
    
    return ProductResponse(**product.to_dict())


@router.put("/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: str,
    product_data: ProductUpdate,
    session: AsyncSession = Depends(get_session)
):
    """Update a product."""
    result = await session.execute(
        select(Product)
        .options(
            selectinload(Product.folder_groups).selectinload(FolderGroup.folder_paths)
        )
        .where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()
    
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )
    
    # Update fields
    if product_data.product_name is not None:
        product.product_name = product_data.product_name
    
    if product_data.description is not None:
        product.description = product_data.description
    
    await session.commit()
    await session.refresh(product)
    
    logger.info("Product updated", product_id=product_id)
    
    return ProductResponse(**product.to_dict())


@router.delete("/{product_id}")
async def delete_product(
    product_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Delete a product and its RAG index data (ChromaDB + chunk records)."""
    result = await session.execute(
        select(Product).where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )

    # Clear RAG index for this product (ChromaDB + chunk records) before deleting the product
    try:
        from app.api.settings import get_active_llm_settings
        from app.rag.clients import OpenAIEmbeddingClient
        from app.rag.embedding_index import EmbeddingIndexService

        llm = await get_active_llm_settings(session)
        emb_key = llm.get("embeddings_api_key") or llm.get("api_key")
        if emb_key:
            if llm.get("embeddings_api_key"):
                emb_base_url = llm.get("embeddings_api_url") or None
            else:
                emb_base_url = llm.get("embeddings_api_url") or llm.get("api_url") or None
            embedding_client = OpenAIEmbeddingClient(
                api_key=emb_key,
                base_url=emb_base_url,
                model=llm.get("embeddings_model") or "text-embedding-3-small",
            )
            index = EmbeddingIndexService(embedding_client=embedding_client)
            cleared = await index.clear_product(product_id)
            logger.info("product_rag_cleared", product_id=product_id, chunks_cleared=cleared)
    except Exception as exc:
        logger.warning("product_rag_clear_failed", product_id=product_id, error=str(exc))
        # Continue with product delete; orphaned index data can be cleaned up later

    await session.delete(product)
    await session.commit()

    logger.info("Product deleted", product_id=product_id)

    return {"status": "deleted", "product_id": product_id}


# ---------------------------------------------------------------------------
# Product-scoped POD Agent management
# ---------------------------------------------------------------------------


@router.post("/{product_id}/generate-pod")
async def generate_product_pod(
    product_id: str,
    target_os: str = "linux",
    agent_name: str = "",
    session: AsyncSession = Depends(get_session)
):
    """Generate a POD agent installer for this product.

    The agent_name is baked into the config so no setup is needed on first run.
    """
    from app.services.pod_generator import generate_pod_installer

    # Verify product exists
    result = await session.execute(
        select(Product).where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )

    # Validate target_os
    if target_os not in ("windows", "linux"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_os must be one of: windows, linux"
        )

    # Validate agent_name
    agent_name = agent_name.strip()
    if not agent_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="agent_name is required"
        )

    # Check for duplicate agent name
    existing = await session.execute(
        select(Pod).where(Pod.pod_name == agent_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent name '{agent_name}' is already taken. Choose a different name."
        )

    # Create POD record linked to this product
    pod_id = str(uuid4())
    pod = Pod(
        pod_id=pod_id,
        product_id=product_id,
        pod_name=agent_name,
        os_type=target_os,
        status="pending",
        metadata_json={"product_name": product.product_name},
    )
    session.add(pod)
    await session.commit()

    logger.info(
        "Generating product POD installer",
        product_id=product_id,
        pod_id=pod_id,
        agent_name=agent_name,
        target_os=target_os,
    )

    # Generate installer package with the name baked in
    installer_info = await generate_pod_installer(
        pod_id=pod_id,
        pod_name=agent_name,
        target_os=target_os,
        custom_config={"product_id": product_id},
    )

    return {
        "pod_id": pod_id,
        "product_id": product_id,
        "agent_name": agent_name,
        "target_os": target_os,
        "archive_name": installer_info["archive_name"],
        "binary_included": installer_info["binary_included"],
        "download_url": installer_info["download_url"],
        "instructions": installer_info["instructions"],
    }


@router.get("/{product_id}/pods")
async def list_product_pods(
    product_id: str,
    session: AsyncSession = Depends(get_session)
):
    """List all POD agents associated with this product."""
    result = await session.execute(
        select(Pod).where(Pod.product_id == product_id)
    )
    pods = result.scalars().all()

    return {
        "product_id": product_id,
        "pods": [p.to_dict() for p in pods],
        "total": len(pods),
        "online": sum(1 for p in pods if p.status == "online"),
    }


@router.post("/{product_id}/groups", response_model=FolderGroupResponse, status_code=status.HTTP_201_CREATED)
async def add_folder_group(
    product_id: str,
    group_data: FolderGroupCreate,
    session: AsyncSession = Depends(get_session)
):
    """Add a folder group to a product."""
    # Verify product exists and eagerly load folder_groups relationship
    result = await session.execute(
        select(Product)
        .options(selectinload(Product.folder_groups))
        .where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()
    
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )
    
    # Verify POD exists
    result = await session.execute(
        select(Pod).where(Pod.pod_id == group_data.pod_id)
    )
    pod = result.scalar_one_or_none()
    
    if not pod:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"POD {group_data.pod_id} not found"
        )
    
    # Generate namespace
    namespace = f"product-{product_id[:8]}-{group_data.group_name.lower().replace(' ', '-')}"
    
    # Check namespace uniqueness
    existing = await session.execute(
        select(FolderGroup).where(FolderGroup.namespace == namespace)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Namespace {namespace} already exists"
        )
    
    # For __local__ pod, validate paths are under allowed browse roots
    if group_data.pod_id == "__local__":
        for path_data in group_data.folder_paths:
            if not is_path_allowed(path_data.absolute_path):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Path is not under allowed browse roots: {path_data.absolute_path}",
                )
    
    # Create folder group
    group_id = str(uuid4())
    group = FolderGroup(
        group_id=group_id,
        product_id=product_id,
        pod_id=group_data.pod_id,
        group_name=group_data.group_name,
        group_type=group_data.group_type,
        namespace=namespace,
    )
    
    # For __local__ pod, validate paths are under allowed browse roots
    if group_data.pod_id == "__local__":
        for path_data in group_data.folder_paths:
            if not is_path_allowed(path_data.absolute_path):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Path is not under allowed browse roots: {path_data.absolute_path}",
                )

    # Add folder paths
    for path_data in group_data.folder_paths:
        folder_path = FolderPath(
            path_id=str(uuid4()),
            group_id=group_id,
            absolute_path=path_data.absolute_path,
            scan_recursive=path_data.scan_recursive,
            file_filters=path_data.file_filters,
        )
        group.folder_paths.append(folder_path)
    
    # Add group to session - the relationship will be maintained via product_id foreign key
    session.add(group)
    await session.commit()
    
    # Reload with relationships
    result = await session.execute(
        select(FolderGroup)
        .options(selectinload(FolderGroup.folder_paths))
        .where(FolderGroup.group_id == group_id)
    )
    group = result.scalar_one()
    
    logger.info(
        "Folder group added to product",
        product_id=product_id,
        group_id=group_id,
        group_name=group_data.group_name
    )
    
    return FolderGroupResponse(**group.to_dict())


@router.delete("/{product_id}/groups/{group_id}")
async def remove_folder_group(
    product_id: str,
    group_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Remove a folder group from a product."""
    result = await session.execute(
        select(FolderGroup).where(
            FolderGroup.group_id == group_id,
            FolderGroup.product_id == product_id
        )
    )
    group = result.scalar_one_or_none()
    
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Folder group {group_id} not found in product {product_id}"
        )
    
    await session.delete(group)
    await session.commit()
    
    logger.info("Folder group removed from product", product_id=product_id, group_id=group_id)
    
    return {"status": "deleted", "group_id": group_id}


@router.post("/{product_id}/train")
async def train_product(
    product_id: str,
    force_full: bool = False,
    session: AsyncSession = Depends(get_session)
):
    """Train a product – runs the RAG pipeline over all its folder groups.

    By default training is **incremental**: only new and modified files
    are processed.  Pass ``?force_full=true`` to wipe the existing index
    and re-process every file from scratch.
    """
    import asyncio
    from app.rag.pipeline import run_product_training

    # Get product with all folder groups and their paths
    result = await session.execute(
        select(Product)
        .options(
            selectinload(Product.folder_groups).selectinload(FolderGroup.folder_paths)
        )
        .where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )

    if not product.folder_groups:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product has no folder groups to train"
        )

    # Collect all active folder paths and group ids
    folder_paths: list[str] = []
    group_ids: list[str] = []
    for group in product.folder_groups:
        if group.training_status == "training":
            continue
        group_ids.append(group.group_id)
        for fp in group.folder_paths:
            if fp.is_active:
                folder_paths.append(fp.absolute_path)

    if not folder_paths:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active folder paths found (or all groups are already training)."
        )

    # Mark groups as training
    for group in product.folder_groups:
        if group.group_id in group_ids:
            group.training_status = "training"
    await session.commit()

    # Launch the 4-phase pipeline in the background
    asyncio.create_task(
        run_product_training(
            product_id=product_id,
            folder_paths=folder_paths,
            group_ids=group_ids,
            product_name=product.product_name,
            product_description=product.description,
            force_full=force_full,
        )
    )

    logger.info(
        "product_training_launched",
        product_id=product_id,
        product_name=product.product_name,
        groups=len(group_ids),
        paths=len(folder_paths),
        force_full=force_full,
    )

    return {
        "status": "started",
        "product_id": product_id,
        "folder_groups": len(group_ids),
        "folder_paths": len(folder_paths),
        "incremental": not force_full,
    }


@router.post("/{product_id}/stop-training")
async def stop_training(
    product_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Stop an in-progress training for a product.
    
    If the background task is still running, cancels it.
    Also resets any stuck 'training' group statuses back to 'pending'.
    """
    from app.rag.pipeline import _active_training_tasks

    task = _active_training_tasks.get(product_id)
    if task and not task.done():
        task.cancel()
        logger.info("product_training_task_cancelled", product_id=product_id)

    # Force-reset any groups stuck in "training" status for this product
    result = await session.execute(
        select(Product)
        .options(selectinload(Product.folder_groups))
        .where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )

    reset_count = 0
    for group in product.folder_groups:
        if group.training_status == "training":
            group.training_status = "pending"
            # Preserve existing metadata but update phase
            meta = group.metadata_json or {}
            existing_logs = meta.get("logs", [])
            from datetime import datetime as dt
            group.metadata_json = {
                **meta,
                "phase": "cancelled",
                "message": "Training stopped by user",
                "logs": existing_logs + [f"[{dt.utcnow().strftime('%H:%M:%S')}] 🛑 Training stopped by user"],
            }
            reset_count += 1

    await session.commit()
    logger.info("product_training_stopped", product_id=product_id, groups_reset=reset_count)

    return {"status": "stopped", "product_id": product_id, "groups_reset": reset_count}


@router.post("/{product_id}/sync-qa")
async def sync_qa_to_training(
    product_id: str,
    body: SyncQARequest,
    session: AsyncSession = Depends(get_session),
):
    """Sync agent Q&A pairs into the product's training index.

    Clears any previously synced Q&A chunks, then embeds and indexes
    the provided pairs so they become searchable by the RAG pipeline.
    """
    from app.api.settings import get_active_llm_settings
    from app.db.database import async_session_maker
    from app.rag.clients import OpenAIEmbeddingClient
    from app.rag.embedding_index import EmbeddingIndexService
    from app.rag.models import Chunk, ChunkMetadata, ProcessingType

    # Verify product exists
    result = await session.execute(
        select(Product).where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found",
        )

    # Load embedding settings
    async with async_session_maker() as settings_session:
        llm = await get_active_llm_settings(settings_session)

    api_key = llm.get("api_key")
    emb_key = llm.get("embeddings_api_key") or api_key
    if not emb_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No embedding API key configured. Set one in Settings.",
        )

    if llm.get("embeddings_api_key"):
        emb_base_url = llm.get("embeddings_api_url") or None
    else:
        emb_base_url = llm.get("embeddings_api_url") or llm.get("api_url") or None

    embedding_client = OpenAIEmbeddingClient(
        api_key=emb_key,
        base_url=emb_base_url,
        model=llm.get("embeddings_model") or "text-embedding-3-small",
    )
    index = EmbeddingIndexService(embedding_client=embedding_client)

    # Clear previously synced Q&A chunks for this product (from Chroma)
    old_paths = await index.get_source_paths_by_prefix(product_id, "agent-qa://")
    if old_paths:
        await index.clear_file_chunks(product_id, old_paths)

    # Build chunks from Q&A pairs
    chunks: list[Chunk] = []
    for pair in body.qa_pairs:
        chunks.append(
            Chunk(
                id=str(uuid4()),
                source_path=f"agent-qa://{uuid4()}",
                processing_type=ProcessingType.DOC,
                text=f"Q: {pair.question}\n\nA: {pair.answer}",
                metadata=ChunkMetadata(
                    sub_category="agent-conversation",
                    component="agent-qa",
                    file_description=pair.question[:100],
                ),
            )
        )

    # Embed and index
    indexed = await index.index_chunks(product_id, chunks)

    logger.info(
        "qa_synced_to_training",
        product_id=product_id,
        pairs=len(body.qa_pairs),
        indexed=indexed,
    )

    return {"synced": indexed, "product_id": product_id}


@router.get("/{product_id}/training-status")
async def get_training_status(
    product_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Get real-time training status for a product's folder groups."""
    # Expire all cached objects so we read fresh progress from the DB
    session.expire_all()
    
    result = await session.execute(
        select(Product)
        .options(
            selectinload(Product.folder_groups).selectinload(FolderGroup.folder_paths)
        )
        .where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )

    groups_status = []
    for group in product.folder_groups:
        meta = group.metadata_json if hasattr(group, 'metadata_json') and group.metadata_json else {}
        groups_status.append({
            "group_id": group.group_id,
            "group_name": group.group_name,
            "group_type": group.group_type,
            "training_status": group.training_status,
            "last_trained": str(group.last_trained) if group.last_trained else None,
            "folder_paths": [fp.absolute_path for fp in group.folder_paths if fp.is_active],
            "stats": meta,
        })

    is_any_training = any(g["training_status"] == "training" for g in groups_status)
    all_completed = all(g["training_status"] == "completed" for g in groups_status) and len(groups_status) > 0
    any_failed = any(g["training_status"] == "failed" for g in groups_status)

    return {
        "product_id": product_id,
        "product_name": product.product_name,
        "is_training": is_any_training,
        "all_completed": all_completed,
        "any_failed": any_failed,
        "groups": groups_status,
    }


@router.get("/{product_id}/training-tree")
async def get_training_tree(
    product_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Return the training data as a hierarchical tree for visualization (from Chroma).

    Structure: processing_type → sub_category → files (with chunk counts).
    """
    from app.api.settings import get_active_llm_settings
    from app.db.database import async_session_maker
    from app.rag.clients import OpenAIEmbeddingClient
    from app.rag.embedding_index import EmbeddingIndexService

    # Verify product exists
    result = await session.execute(
        select(Product).where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )

    async with async_session_maker() as settings_session:
        llm = await get_active_llm_settings(settings_session)
    api_key = llm.get("api_key") or ""
    emb_key = llm.get("embeddings_api_key") or api_key
    emb_base_url = llm.get("embeddings_api_url") or llm.get("api_url") or None
    embedding_client = OpenAIEmbeddingClient(
        api_key=emb_key,
        base_url=emb_base_url,
        model=llm.get("embeddings_model") or "text-embedding-3-small",
    )
    index = EmbeddingIndexService(embedding_client=embedding_client)
    rows = await index.get_training_tree_data(product_id)

    # Build the tree: type → sub_category → files
    type_map: dict = {}
    total_chunks = 0
    total_files = 0

    for ptype, path, count, sub_cat in rows:
        total_chunks += count

        if ptype not in type_map:
            type_map[ptype] = {"chunks": 0, "files": 0, "sub_categories": {}}

        type_map[ptype]["chunks"] += count

        if sub_cat not in type_map[ptype]["sub_categories"]:
            type_map[ptype]["sub_categories"][sub_cat] = {"chunks": 0, "files": []}

        short_name = path.split("/")[-1] if "/" in path else path
        type_map[ptype]["sub_categories"][sub_cat]["chunks"] += count
        type_map[ptype]["sub_categories"][sub_cat]["files"].append({
            "path": path,
            "name": short_name,
            "chunks": count,
        })

    for ptype, data in type_map.items():
        file_count = 0
        for sub_cat, sub_data in data["sub_categories"].items():
            file_count += len(sub_data["files"])
        data["files"] = file_count
        total_files += file_count

    # Build ordered tree array
    type_order = ["expert_qa", "summary", "code", "doc", "ticket_export", "diagram_image", "doc_with_diagrams", "other"]
    tree = []
    for ptype in type_order:
        if ptype in type_map:
            sub_cats = []
            for sub_cat, sub_data in sorted(type_map[ptype]["sub_categories"].items()):
                sub_cats.append({
                    "name": sub_cat,
                    "chunks": sub_data["chunks"],
                    "files": sorted(sub_data["files"], key=lambda f: f["name"]),
                    "file_count": len(sub_data["files"]),
                })
            tree.append({
                "type": ptype,
                "chunks": type_map[ptype]["chunks"],
                "file_count": type_map[ptype]["files"],
                "sub_categories": sub_cats,
            })
    # Add any types not in the order list
    for ptype in type_map:
        if ptype not in type_order:
            sub_cats = []
            for sub_cat, sub_data in sorted(type_map[ptype]["sub_categories"].items()):
                sub_cats.append({
                    "name": sub_cat,
                    "chunks": sub_data["chunks"],
                    "files": sorted(sub_data["files"], key=lambda f: f["name"]),
                    "file_count": len(sub_data["files"]),
                })
            tree.append({
                "type": ptype,
                "chunks": type_map[ptype]["chunks"],
                "file_count": type_map[ptype]["files"],
                "sub_categories": sub_cats,
            })

    return {
        "product_id": product_id,
        "product_name": product.product_name,
        "total_chunks": total_chunks,
        "total_files": total_files,
        "tree": tree,
    }


@router.get("/{product_id}/chunks")
async def list_product_chunks(
    product_id: str,
    processing_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession = Depends(get_session)
):
    """List indexed chunks for a product (from Chroma)."""
    from app.api.settings import get_active_llm_settings
    from app.db.database import async_session_maker
    from app.rag.clients import OpenAIEmbeddingClient
    from app.rag.embedding_index import EmbeddingIndexService

    result = await session.execute(
        select(Product).where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )

    async with async_session_maker() as settings_session:
        llm = await get_active_llm_settings(settings_session)
    api_key = llm.get("api_key") or ""
    emb_key = llm.get("embeddings_api_key") or api_key
    emb_base_url = llm.get("embeddings_api_url") or llm.get("api_url") or None
    embedding_client = OpenAIEmbeddingClient(
        api_key=emb_key,
        base_url=emb_base_url,
        model=llm.get("embeddings_model") or "text-embedding-3-small",
    )
    index = EmbeddingIndexService(embedding_client=embedding_client)
    chunk_dicts, total = await index.get_chunks_list(
        product_id, processing_type=processing_type, limit=limit, offset=offset
    )

    return {
        "product_id": product_id,
        "product_name": product.product_name,
        "total": total,
        "limit": limit,
        "offset": offset,
        "chunks": chunk_dicts,
    }
