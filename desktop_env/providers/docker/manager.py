import os
import platform
import sys
import zipfile
import glob
from pathlib import Path
from typing import List, Optional, Set, Union

from time import sleep
import requests
from tqdm import tqdm

import logging

from desktop_env.providers.base import VMManager

logger = logging.getLogger("desktopenv.providers.docker.DockerVMManager")
logger.setLevel(logging.INFO)

MAX_RETRY_TIMES = 10
RETRY_INTERVAL = 5

UBUNTU_X86_URL = "https://huggingface.co/datasets/xlangai/ubuntu_osworld/resolve/main/Ubuntu.qcow2.zip"
WINDOWS_X86_URL = "https://huggingface.co/datasets/xlangai/windows_osworld/resolve/main/Windows-10-x64.qcow2.zip"

_VM_FILE_MARKERS = (
    "Ubuntu.qcow2",
    "Windows-10-x64.qcow2",
    "Ubuntu.qcow2.zip",
    "Windows-10-x64.qcow2.zip",
)


def _normalize_path(path: Optional[Union[os.PathLike, str]]) -> Optional[str]:
    if not path:
        return None
    try:
        return str(Path(path).expanduser().resolve())
    except Exception:
        try:
            return os.path.abspath(os.path.expanduser(str(path)))
        except Exception:
            return None


def _is_vm_dir_ready(path: str) -> bool:
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return False
    return any((p / marker).exists() for marker in _VM_FILE_MARKERS)


def _collect_candidate_vm_dirs(explicit_vm_dir: Optional[str] = None) -> List[str]:
    """Collect candidate VM directories with de-duplication while keeping priority order."""
    candidates: List[str] = []
    seen: Set[str] = set()

    def add(raw_path: Optional[Union[os.PathLike, str]]) -> None:
        normalized = _normalize_path(raw_path)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    # 1) Explicit path (highest priority)
    add(explicit_vm_dir)

    # 2) Environment variables
    for key in ("OSGYM_VM_DIR"):
        add(os.environ.get(key))

    # 3) Derive from common roots (__file__, cwd, sys.path, PYTHONPATH)
    roots: List[Path] = []
    roots.extend(Path(__file__).resolve().parents)

    cwd = Path.cwd()
    roots.append(cwd)
    roots.extend(cwd.parents)

    for p in sys.path:
        if not p:
            continue
        try:
            roots.append(Path(p).expanduser().resolve())
        except Exception:
            continue

    for p in (os.environ.get("PYTHONPATH", "") or "").split(os.pathsep):
        if not p:
            continue
        try:
            roots.append(Path(p).expanduser().resolve())
        except Exception:
            continue

    for root in roots:
        add(root / "env" / "osgym" / "docker_vm_data")
        add(root / "AIEvoBox" / "env" / "osgym" / "docker_vm_data")
        if root.name == "docker_vm_data":
            add(root)

    # 4) Shared storage conventions (username may vary)
    shared_patterns = (
        "/mnt/shared-storage-user/evobox-share/*/projects/AIEvoBox/env/osgym/docker_vm_data"
    )
    for pattern in shared_patterns:
        for path in glob.glob(pattern):
            add(path)

    # 5) Backward-compatible defaults
    add("/root/AIEvoBox/env/osgym/docker_vm_data")
    add(os.path.join(os.getcwd(), "env", "osgym", "docker_vm_data"))
    add("/mnt/shared-storage-user/evobox-share/zhangyang/projects/AIEvoBox/env/osgym/docker_vm_data")

    return candidates


def _find_vm_dir(explicit_vm_dir: Optional[str] = None) -> str:
    """Find VM storage dir with robust discovery and env/path fallback."""
    candidate_paths = _collect_candidate_vm_dirs(explicit_vm_dir=explicit_vm_dir)

    # Prefer directories that already contain qcow2/zip markers.
    for path in candidate_paths:
        if _is_vm_dir_ready(path):
            logger.info("Found VM directory with existing VM files: %s", path)
            return path

    # Fallback to any existing directory candidate.
    for path in candidate_paths:
        if os.path.isdir(path):
            logger.info("Found VM directory: %s", path)
            return path

    default_path = (
        _normalize_path(explicit_vm_dir)
        or _normalize_path(os.environ.get("OSGYM_VM_DIR"))
        or "/root/AIEvoBox/env/osgym/docker_vm_data"
    )
    logger.warning("No existing VM directory found, will use: %s", default_path)
    return default_path

URL = UBUNTU_X86_URL
DOWNLOADED_FILE_NAME = URL.split('/')[-1]

if platform.system() == 'Windows':
    docker_path = r"C:\Program Files\Docker\Docker"
    os.environ["PATH"] += os.pathsep + docker_path


def _download_vm(vms_dir: str):
    global URL, DOWNLOADED_FILE_NAME
    # Download the virtual machine image
    logger.info("Downloading the virtual machine image...")
    downloaded_size = 0

    downloaded_file_name = DOWNLOADED_FILE_NAME

    os.makedirs(vms_dir, exist_ok=True)

    while True:
        downloaded_file_path = os.path.join(vms_dir, downloaded_file_name)
        headers = {}
        if os.path.exists(downloaded_file_path):
            downloaded_size = os.path.getsize(downloaded_file_path)
            headers["Range"] = f"bytes={downloaded_size}-"

        with requests.get(URL, headers=headers, stream=True) as response:
            if response.status_code == 416:
                # This means the range was not satisfiable, possibly the file was fully downloaded
                logger.info("Fully downloaded or the file size changed.")
                break

            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))

            with open(downloaded_file_path, "ab") as file, tqdm(
                    desc="Progress",
                    total=total_size,
                    unit='iB',
                    unit_scale=True,
                    unit_divisor=1024,
                    initial=downloaded_size,
                    ascii=True
            ) as progress_bar:
                try:
                    for data in response.iter_content(chunk_size=1024):
                        size = file.write(data)
                        progress_bar.update(size)
                except (requests.exceptions.RequestException, IOError) as e:
                    logger.error(f"Download error: {e}")
                    sleep(RETRY_INTERVAL)
                    logger.error("Retrying...")
                else:
                    logger.info("Download succeeds.")
                    break  # Download completed successfully

    if downloaded_file_name.endswith(".zip"):
        # Unzip the downloaded file
        logger.info("Unzipping the downloaded file...")
        with zipfile.ZipFile(downloaded_file_path, 'r') as zip_ref:
            zip_ref.extractall(vms_dir)
        logger.info("Files have been successfully extracted to the directory: " + str(vms_dir))


class DockerVMManager(VMManager):
    def __init__(self, registry_path="", vm_dir: Optional[str] = None):
        self.registry_path = registry_path
        # Resolve lazily at instance creation time so actor/runtime env vars can take effect.
        self.vms_dir = _find_vm_dir(explicit_vm_dir=vm_dir)
        logger.info("DockerVMManager initialized with VM directory: %s", self.vms_dir)

    def add_vm(self, vm_path):
        pass

    def check_and_clean(self):
        pass

    def delete_vm(self, vm_path):
        pass

    def initialize_registry(self):
        pass

    def list_free_vms(self):
        return os.path.join(self.vms_dir, DOWNLOADED_FILE_NAME)

    def occupy_vm(self, vm_path):
        pass

    def get_vm_path(self, os_type, region):
        global URL, DOWNLOADED_FILE_NAME
        if os_type == "Ubuntu":
            URL = UBUNTU_X86_URL
        elif os_type == "Windows":
            URL = WINDOWS_X86_URL
        DOWNLOADED_FILE_NAME = URL.split('/')[-1]

        if DOWNLOADED_FILE_NAME.endswith(".zip"):
            vm_name = DOWNLOADED_FILE_NAME[:-4]
        else:
            vm_name = DOWNLOADED_FILE_NAME

        vm_path = os.path.join(self.vms_dir, vm_name)
        if not os.path.exists(vm_path):
            _download_vm(self.vms_dir)
        return vm_path
