"""
Extension registry — single source of truth for file-type classification.

Phase 1 uses EXCLUDE_EXTENSIONS to drop files that are definitely not
text or image (binaries, archives, compiled objects, audio, video, …).

The sets here are intentionally comprehensive.  Sources:
  - bevry/binaryextensions (GitHub)
  - MDN image type guide
  - file-extensions.org
  - manual additions for enterprise / mainframe / SAP / ERP formats
"""

# ── Phase 1: extensions that are DEFINITELY NOT text or image ──────────────
# These files cannot meaningfully contribute to a knowledge base.
EXCLUDE_EXTENSIONS: frozenset[str] = frozenset({
    # ── Executables & installers ───────────────────────────────────────
    ".exe", ".msi", ".app", ".dmg", ".deb", ".rpm", ".pkg", ".snap",
    ".flatpak", ".appimage", ".com", ".scr", ".gadget", ".cpl",
    ".inf", ".sys",

    # ── Archives & compressed ──────────────────────────────────────────
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz",
    ".cab", ".iso", ".img", ".z", ".lz", ".lz4", ".zst", ".br",
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",

    # ── Compiled / object / bytecode ───────────────────────────────────
    ".o", ".a", ".so", ".dylib", ".dll", ".lib", ".class", ".pyc",
    ".pyo", ".wasm", ".beam", ".pdb", ".ilk", ".exp", ".obj",
    ".ko", ".elf",

    # ── Audio ──────────────────────────────────────────────────────────
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma",
    ".opus", ".mid", ".midi", ".aiff", ".ape", ".ra",

    # ── Video ──────────────────────────────────────────────────────────
    ".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".webm",
    ".m4v", ".3gp", ".mpg", ".mpeg", ".vob", ".ts", ".m2ts",
    ".ogv", ".rm", ".rmvb", ".asf",

    # ── Fonts ──────────────────────────────────────────────────────────
    ".ttf", ".otf", ".woff", ".woff2", ".eot", ".fon", ".fnt",

    # ── Database files ─────────────────────────────────────────────────
    ".db", ".sqlite", ".sqlite3", ".mdb", ".ldb", ".accdb",
    ".db-wal", ".db-shm", ".db-journal",
    ".frm", ".myd", ".myi", ".ibd",

    # ── ML / data serialization (binary) ───────────────────────────────
    ".bin", ".dat", ".pickle", ".pkl", ".npy", ".npz",
    ".h5", ".hdf5", ".onnx", ".pb", ".pt", ".pth",
    ".safetensors", ".gguf", ".parquet", ".arrow", ".feather",
    ".tfrecord", ".tflite", ".mar",

    # ── Package manager locks (huge, not useful) ───────────────────────
    ".lock",

    # ── Source maps & minified bundles ─────────────────────────────────
    ".map", ".min.js", ".min.css",

    # ── Disk / VM images ───────────────────────────────────────────────
    ".vmdk", ".vdi", ".vhd", ".vhdx", ".qcow2", ".ova", ".ovf",

    # ── Crypto / certificates (binary) ─────────────────────────────────
    ".p12", ".pfx", ".jks", ".keystore", ".cer", ".der",

    # ── Java / .NET / Go archives ──────────────────────────────────────
    ".jar", ".war", ".ear", ".nupkg", ".snupkg",

    # ── Flash / legacy ─────────────────────────────────────────────────
    ".swf", ".fla",

    # ── Backup / temp ──────────────────────────────────────────────────
    ".bak", ".tmp", ".temp", ".swp", ".swo",

    # ── OS junk ────────────────────────────────────────────────────────
    ".ds_store", ".thumbs.db",

    # ── SAP / ERP binary transports ────────────────────────────────────
    ".cofile", ".data",
})

# Directories that should never appear in the tree at all.
# These are universally non-useful (version-control internals, OS metadata,
# package manager dirs that often contain symlink loops and huge file counts).
IGNORE_DIRS: frozenset[str] = frozenset({
    ".git", ".svn", ".hg", ".bzr", "_darcs", ".fossil",
    "__MACOSX", "$RECYCLE.BIN", "System Volume Information",
    ".Spotlight-V100", ".Trashes", ".fseventsd",
    ".DS_Store",
    "node_modules",  # npm/yarn; often contains circular symlinks (e.g. workspace links)
})
