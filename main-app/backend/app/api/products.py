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
    """Delete a product."""
    result = await session.execute(
        select(Product).where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )

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

    Embedding-based sync is disabled. Use KB-based training instead.
    """
    result = await session.execute(
        select(Product).where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found",
        )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Q&A sync to embedding index is disabled. Use KB-based training.",
    )


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
    """Return the training data as a hierarchical tree for visualization.

    Reads the real KnowledgeBase blob and builds the tree from file_analysis,
    folder_structure, and files stored during training.
    """
    result = await session.execute(
        select(Product).where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )

    # Load the actual KB data
    from app.rag.kb_store import KnowledgeBaseStore
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(product_id)

    if not kb_data:
        return {
            "product_id": product_id,
            "product_name": product.product_name,
            "total_chunks": 0,
            "total_files": 0,
            "tree": [],
        }

    files = kb_data.get("files", {})
    file_analysis = kb_data.get("file_analysis", {})
    chunk_map = kb_data.get("chunk_map", {})

    # Classify each file into a type using file_analysis or extension
    type_buckets: dict[str, dict[str, list]] = {}  # {type: {sub_category: [file_info]}}

    for file_path, file_data in files.items():
        # Determine processing type from file_analysis or extension
        fa = file_analysis.get(file_path, {})
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""

        # Guess type from component/role or extension
        if ext in ("py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "cpp", "h", "cs", "rb", "php", "swift", "kt"):
            ptype = "code"
        elif ext in ("md", "txt", "rst", "adoc", "docx", "pdf", "html"):
            ptype = "doc"
        elif ext in ("png", "jpg", "jpeg", "gif", "svg", "webp", "bmp"):
            ptype = "diagram_image"
        elif ext in ("csv", "json", "xml", "yaml", "yml", "toml", "ini", "cfg", "conf"):
            ptype = "doc"
        else:
            ptype = "other"

        # Derive sub-category from folder path
        parts = file_path.replace("\\", "/").split("/")
        if len(parts) > 1:
            sub_cat = parts[-2] if len(parts) >= 2 else "root"
        else:
            sub_cat = "root"

        # Count chunks for this file
        file_chunk_count = sum(
            1 for cm in chunk_map.values()
            if isinstance(cm, dict) and cm.get("file") == file_path
        )

        file_info = {
            "path": file_path,
            "name": parts[-1] if parts else file_path,
            "chunks": file_chunk_count,
        }

        if ptype not in type_buckets:
            type_buckets[ptype] = {}
        if sub_cat not in type_buckets[ptype]:
            type_buckets[ptype][sub_cat] = []
        type_buckets[ptype][sub_cat].append(file_info)

    # Also count non-file chunks (folder_analysis, project_analysis)
    analysis_chunks = sum(
        1 for cm in chunk_map.values()
        if isinstance(cm, dict) and cm.get("type") in ("folder_analysis", "project_analysis", "file_analysis")
    )
    if analysis_chunks > 0:
        if "summary" not in type_buckets:
            type_buckets["summary"] = {}
        type_buckets["summary"]["auto-generated"] = [{
            "path": "(analysis)",
            "name": f"{analysis_chunks} analysis entries",
            "chunks": analysis_chunks,
        }]

    # Build tree response
    tree = []
    total_chunks = len(chunk_map)
    total_files = len(files)

    for ptype, sub_cats in type_buckets.items():
        type_chunks = sum(f["chunks"] for fs in sub_cats.values() for f in fs)
        type_file_count = sum(len(fs) for fs in sub_cats.values())
        sub_categories = []
        for sub_name, file_list in sorted(sub_cats.items()):
            sub_chunks = sum(f["chunks"] for f in file_list)
            sub_categories.append({
                "name": sub_name,
                "chunks": sub_chunks,
                "file_count": len(file_list),
                "files": sorted(file_list, key=lambda x: x["name"]),
            })
        tree.append({
            "type": ptype,
            "chunks": type_chunks,
            "file_count": type_file_count,
            "sub_categories": sub_categories,
        })

    # Sort: code first, then doc, then others
    type_order = {"code": 0, "doc": 1, "summary": 2, "diagram_image": 3, "other": 4}
    tree.sort(key=lambda t: type_order.get(t["type"], 99))

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
    """List indexed chunks for a product. Returns empty list when embedding index is not used."""
    result = await session.execute(
        select(Product).where(Product.product_id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found"
        )

    return {
        "product_id": product_id,
        "product_name": product.product_name,
        "total": 0,
        "limit": limit,
        "offset": offset,
        "chunks": [],
    }
