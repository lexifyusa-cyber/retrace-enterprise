"""
POD installer generator service.
"""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Optional

import structlog

from app.core.config import settings

logger = structlog.get_logger()


async def generate_pod_installer(
    pod_id: str,
    pod_name: str,
    target_os: str,
    custom_config: Optional[dict] = None
) -> dict:
    """
    Generate a POD installer package for a specific platform.
    
    Creates a downloadable package containing:
    1. POD agent binary for the target OS
    2. Configuration file pre-configured for this POD
    3. Simple README with run instructions
    """
    
    # Create output directory
    output_dir = Path("./pod-installers")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Create temporary staging directory for this POD
    stage_dir = output_dir / f"pod-{pod_id[:8]}"
    stage_dir.mkdir(exist_ok=True)
    
    # Generate configuration
    config = {
        "pod": {
            "id": pod_id,
            "name": pod_name,
        },
        "main_app": {
            "url": f"ws://localhost:{settings.WEBSOCKET_PORT}",
            "reconnect_interval_seconds": 30,
        },
        "storage": {
            "data_dir": "./pod-data",
            "vector_db": {
                "type": "chromadb",
                "path": "./pod-data/vector.db"
            },
            "metadata_db": {
                "type": "sqlite",
                "path": "./pod-data/metadata.db"
            }
        },
        "resources": {
            "max_memory_mb": custom_config.get("max_memory_mb", 2048) if custom_config else 2048,
            "max_cpu_percent": custom_config.get("max_cpu_percent", 50) if custom_config else 50,
        },
        "file_watcher": {
            "enabled": True,
            "debounce_seconds": 5,
        }
    }
    
    # Write configuration file to staging directory
    import yaml
    config_path = stage_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    
    # Build or copy POD binary
    binary_name = "retrace-agent"
    if target_os == "windows":
        binary_name += ".exe"
    
    binary_path = await _get_or_build_binary(target_os, stage_dir / binary_name)
    
    if not binary_path or not binary_path.exists():
        logger.warning(
            "POD binary not available, creating config-only package",
            target_os=target_os
        )
        binary_available = False
    else:
        binary_available = True
    
    # Generate simple README with run instructions
    if target_os == "linux":
        instructions = f"""# ReTrace — {pod_name}

## Quick Start (Linux & macOS)

1. Extract this archive:
   tar -xzf retrace-*.tar.gz
2. Make executable:
   chmod +x {binary_name}
3. Run the agent:
   ./{binary_name} --config config.yaml

The wrapper script auto-detects your platform (Linux or macOS, Intel or ARM).

## Running in Background

   nohup ./{binary_name} --config config.yaml &
"""
    elif target_os == "macos":
        instructions = f"""# ReTrace — {pod_name}

## Quick Start

1. Extract this archive
2. Make the binary executable:
   chmod +x {binary_name}
3. Run the agent:
   ./{binary_name} --config config.yaml

## Running in Background

   nohup ./{binary_name} --config config.yaml &
"""
    else:  # windows
        instructions = f"""# ReTrace — {pod_name}

## Quick Start

1. Extract this archive
2. Run the agent:
   {binary_name} --config config.yaml

## Running as Service

See documentation for Windows Service installation.
"""
    
    # Write README
    readme_path = stage_dir / "README.txt"
    with open(readme_path, "w") as f:
        f.write(instructions)
    
    # Create archive
    archive_name = f"retrace-{pod_id[:8]}-{target_os}"
    if target_os == "windows":
        archive_path = output_dir / f"{archive_name}.zip"
        _create_zip(stage_dir, archive_path)
    else:
        archive_path = output_dir / f"{archive_name}.tar.gz"
        _create_tarball(stage_dir, archive_path)
    
    # Clean up staging directory
    shutil.rmtree(stage_dir)
    
    logger.info(
        "POD installer package created",
        pod_id=pod_id,
        target_os=target_os,
        archive_path=str(archive_path),
        binary_included=binary_available
    )
    
    return {
        "path": str(archive_path),
        "archive_name": archive_path.name,
        "instructions": instructions,
        "binary_included": binary_available,
        "download_url": f"/api/v1/pods/{pod_id}/download"
    }


async def _get_or_build_binary(target_os: str, output_path: Path) -> Optional[Path]:
    """Get or build the POD binary for the target OS."""
    
    # Map OS names to Go build targets
    # "linux" means Unix (includes both Linux and macOS)
    os_map = {
        "linux": "linux/amd64",
        "macos": "darwin/amd64",
        "windows": "windows/amd64"
    }
    
    if target_os not in os_map:
        return None
    
    # Check if binary already exists in pod-agent/dist
    project_root = Path(__file__).parent.parent.parent.parent.parent
    pod_agent_dir = project_root / "pod-agent"
    
    # For Unix (target_os=linux), include both linux and darwin binaries
    # with a wrapper script that auto-detects the platform
    if target_os == "linux":
        return await _get_or_build_unix_bundle(pod_agent_dir, output_path)
    
    goos, goarch = os_map[target_os].split("/")
    binary_name = f"retrace-agent-{goos}-{goarch}"
    if target_os == "windows":
        binary_name += ".exe"
    
    dist_binary = pod_agent_dir / "dist" / binary_name
    
    # If binary exists and is recent, use it
    if dist_binary.exists():
        logger.info("Using existing binary", path=str(dist_binary))
        shutil.copy2(dist_binary, output_path)
        return output_path
    
    # Try to build the binary
    try:
        logger.info("Building POD binary", target_os=target_os)
        
        # First, ensure dependencies are downloaded (run go mod download)
        logger.info("Downloading Go dependencies")
        tidy_result = subprocess.run(
            ["go", "mod", "tidy"],
            cwd=str(pod_agent_dir),
            capture_output=True,
            timeout=60
        )
        
        if tidy_result.returncode != 0:
            logger.warning(
                "go mod tidy failed, attempting build anyway",
                stderr=tidy_result.stderr.decode()
            )
        
        # Run go build
        env = os.environ.copy()
        env["GOOS"] = goos
        env["GOARCH"] = goarch
        
        result = subprocess.run(
            ["go", "build", "-ldflags=-s -w", "-o", str(output_path), "./cmd/agent"],
            cwd=str(pod_agent_dir),
            env=env,
            capture_output=True,
            timeout=120
        )
        
        if result.returncode == 0 and output_path.exists():
            logger.info("Binary built successfully", path=str(output_path))
            # Make binary executable
            output_path.chmod(0o755)
            return output_path
        else:
            logger.error(
                "Failed to build binary",
                returncode=result.returncode,
                stderr=result.stderr.decode()
            )
            return None
            
    except Exception as e:
        logger.error("Error building binary", error=str(e))
        return None


async def _get_or_build_unix_bundle(pod_agent_dir: Path, output_path: Path) -> Optional[Path]:
    """Bundle linux + darwin binaries into a single directory with a wrapper script.

    The wrapper ``retrace-agent`` auto-detects the OS and runs the right binary.
    """
    output_dir = output_path.parent

    # Candidates: prefer arm64 for darwin (Apple Silicon), amd64 as fallback
    binaries = {
        "linux-amd64": pod_agent_dir / "dist" / "retrace-agent-linux-amd64",
        "darwin-arm64": pod_agent_dir / "dist" / "retrace-agent-darwin-arm64",
        "darwin-amd64": pod_agent_dir / "dist" / "retrace-agent-darwin-amd64",
    }

    found_any = False
    for label, bin_path in binaries.items():
        dest = output_dir / f"bin-{label}"
        if bin_path.exists():
            shutil.copy2(bin_path, dest)
            dest.chmod(0o755)
            found_any = True
            logger.info("Bundled unix binary", label=label)

    if not found_any:
        logger.warning("No pre-built unix binaries found")
        return None

    # Write a wrapper script that auto-detects OS and arch
    wrapper = output_path  # this is where the caller expects the "binary"
    wrapper.write_text(
        '#!/bin/sh\n'
        'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'OS="$(uname -s | tr A-Z a-z)"\n'
        'ARCH="$(uname -m)"\n'
        'case "$OS" in\n'
        '  linux)  OS_LABEL="linux" ;;\n'
        '  darwin) OS_LABEL="darwin" ;;\n'
        '  *)      echo "Unsupported OS: $OS"; exit 1 ;;\n'
        'esac\n'
        'case "$ARCH" in\n'
        '  x86_64|amd64) ARCH_LABEL="amd64" ;;\n'
        '  arm64|aarch64) ARCH_LABEL="arm64" ;;\n'
        '  *)             ARCH_LABEL="amd64" ;;\n'
        'esac\n'
        'BINARY="$SCRIPT_DIR/bin-${OS_LABEL}-${ARCH_LABEL}"\n'
        'if [ ! -f "$BINARY" ]; then\n'
        '  # Fallback: try amd64\n'
        '  BINARY="$SCRIPT_DIR/bin-${OS_LABEL}-amd64"\n'
        'fi\n'
        'if [ ! -f "$BINARY" ]; then\n'
        '  echo "No binary found for $OS/$ARCH"\n'
        '  echo "Available binaries:"\n'
        '  ls "$SCRIPT_DIR"/bin-* 2>/dev/null || echo "  (none)"\n'
        '  exit 1\n'
        'fi\n'
        'exec "$BINARY" "$@"\n'
    )
    wrapper.chmod(0o755)
    logger.info("Created unix wrapper script", path=str(wrapper))

    return wrapper


def _create_zip(source_dir: Path, output_path: Path):
    """Create a zip archive."""
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file in source_dir.rglob('*'):
            if file.is_file():
                arcname = file.relative_to(source_dir)
                zipf.write(file, arcname)


def _create_tarball(source_dir: Path, output_path: Path):
    """Create a tar.gz archive."""
    import tarfile
    
    with tarfile.open(output_path, 'w:gz') as tar:
        for file in source_dir.rglob('*'):
            if file.is_file():
                arcname = file.relative_to(source_dir)
                tar.add(file, arcname=arcname)
