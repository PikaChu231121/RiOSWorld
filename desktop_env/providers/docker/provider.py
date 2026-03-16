import logging
import os
import platform
import time
import docker
import psutil
import requests
from filelock import FileLock
from pathlib import Path

from desktop_env.providers.base import Provider

logger = logging.getLogger("desktopenv.providers.docker.DockerProvider")
logger.setLevel(logging.INFO)

WAIT_TIME = 3
RETRY_INTERVAL = 1
LOCK_TIMEOUT = 10
PUBLIC_IMAGE = "happysixd/osworld-docker:latest"
INTERNAL_IMAGE = "registry.h.pjlab.org.cn/ailab-evobox-evobox_cpu/osworld:v1.0"
COMPATIBLE_IMAGES = (PUBLIC_IMAGE, INTERNAL_IMAGE)


class PortAllocationError(Exception):
    pass


class DockerProvider(Provider):
    def __init__(self, region: str):
        self.client = docker.from_env()
        self.server_port = None
        self.vnc_port = None
        self.chromium_port = None
        self.vlc_port = None
        self.container = None
        self.environment = {"DISK_SIZE": "32G", "RAM_SIZE": "4G", "CPU_CORES": "4"}  # Modify if needed

        temp_dir = Path(os.getenv('TEMP') if platform.system() == 'Windows' else '/tmp')
        self.lock_file = temp_dir / "docker_port_allocation.lck"
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

    def _get_local_compatible_image(self):
        """Return the first compatible image already present locally."""
        for image in COMPATIBLE_IMAGES:
            try:
                self.client.images.get(image)
                logger.info(f"Using local Docker image: {image}")
                return image
            except docker.errors.ImageNotFound:
                continue
            except docker.errors.DockerException as exc:
                logger.warning(f"Failed to inspect local Docker image {image}: {exc}")
        return None

    def _pull_compatible_image(self):
        """Pull a compatible image, preferring the public image first."""
        pull_errors = []
        for image in COMPATIBLE_IMAGES:
            try:
                logger.info(f"Pulling Docker image: {image}")
                self.client.images.pull(image)
                logger.info(f"Pulled Docker image: {image}")
                return image
            except docker.errors.DockerException as exc:
                logger.warning(f"Failed to pull Docker image {image}: {exc}")
                pull_errors.append(f"{image}: {exc}")

        raise RuntimeError(
            "Unable to find or pull a compatible OSWorld Docker image. "
            + "Tried: "
            + "; ".join(pull_errors)
        )

    def _resolve_container_image(self):
        local_image = self._get_local_compatible_image()
        if local_image:
            return local_image

        logger.info(
            "No compatible local Docker image found. Pull order: %s",
            " -> ".join(COMPATIBLE_IMAGES),
        )
        return self._pull_compatible_image()

    def _get_used_ports(self):
        """Get all currently used ports (both system and Docker)."""
        # Get system ports
        system_ports = set(conn.laddr.port for conn in psutil.net_connections())
        
        # Get Docker container ports
        docker_ports = set()
        for container in self.client.containers.list():
            ports = container.attrs['NetworkSettings']['Ports']
            if ports:
                for port_mappings in ports.values():
                    if port_mappings:
                        docker_ports.update(int(p['HostPort']) for p in port_mappings)
        
        return system_ports | docker_ports

    def _get_available_port(self, start_port: int) -> int:
        """Find next available port starting from start_port."""
        used_ports = self._get_used_ports()
        port = start_port
        while port < 65354:
            if port not in used_ports:
                return port
            port += 1
        raise PortAllocationError(f"No available ports found starting from {start_port}")

    def _wait_for_vm_ready(self, timeout: int = 300):
        """Wait for VM to be ready by checking screenshot endpoint."""
        start_time = time.time()
        
        def check_screenshot():
            try:
                response = requests.get(
                    f"http://localhost:{self.server_port}/screenshot",
                    timeout=(10, 10)
                )
                return response.status_code == 200
            except Exception:
                return False

        while time.time() - start_time < timeout:
            if check_screenshot():
                return True
            logger.info("Checking if virtual machine is ready...")
            time.sleep(RETRY_INTERVAL)
        
        raise TimeoutError("VM failed to become ready within timeout period")

    def start_emulator(self, path_to_vm: str, headless: bool, os_type: str):
        image_name = self._resolve_container_image()

        # Use a single lock for all port allocation and container startup
        lock = FileLock(str(self.lock_file), timeout=LOCK_TIMEOUT)
        
        try:
            with lock:
                # Allocate all required ports
                self.vnc_port = self._get_available_port(8006)
                self.server_port = self._get_available_port(5000)
                self.chromium_port = self._get_available_port(9222)
                self.vlc_port = self._get_available_port(8080)

                # Start container while still holding the lock
                devices = []
                if os.path.exists("/dev/kvm"):
                    devices.append("/dev/kvm")
                    logger.info("KVM device found, using hardware acceleration")
                else:
                    self.environment["KVM"] = "N"
                    logger.warning("KVM device not found, running without hardware acceleration (will be slower)")

                self.container = self.client.containers.run(
                    image_name,
                    environment=self.environment,
                    cap_add=["NET_ADMIN"],
                    devices=devices,
                    volumes={
                        os.path.abspath(path_to_vm): {
                            "bind": "/System.qcow2",
                            "mode": "ro"
                        }
                    },
                    ports={
                        8006: self.vnc_port,
                        5000: self.server_port,
                        9222: self.chromium_port,
                        8080: self.vlc_port
                    },
                    detach=True
                )

            logger.info(
                f"Started container from image {image_name} with ports - VNC: {self.vnc_port}, "
                f"Server: {self.server_port}, Chrome: {self.chromium_port}, VLC: {self.vlc_port}"
            )

            # Wait for VM to be ready
            self._wait_for_vm_ready()

        except Exception as e:
            # Clean up if anything goes wrong
            if self.container:
                try:
                    self.container.stop()
                    self.container.remove()
                except:
                    pass
            raise e

    def get_ip_address(self, path_to_vm: str) -> str:
        if not all([self.server_port, self.chromium_port, self.vnc_port, self.vlc_port]):
            raise RuntimeError("VM not started - ports not allocated")
        return f"localhost:{self.server_port}:{self.chromium_port}:{self.vnc_port}:{self.vlc_port}"

    def save_state(self, path_to_vm: str, snapshot_name: str):
        raise NotImplementedError("Snapshots not available for Docker provider")

    def revert_to_snapshot(self, path_to_vm: str, snapshot_name: str):
        self.stop_emulator(path_to_vm)

    def stop_emulator(self, path_to_vm: str):
        if self.container:
            container_id = self.container.id[:12]
            logger.info(f"Stopping VM (container {container_id})...")
            try:
                self.container.stop(timeout=10)
            except Exception as e:
                logger.warning(f"container.stop() failed for {container_id}: {e}, trying kill...")
                try:
                    self.container.kill()
                except Exception:
                    pass
            try:
                self.container.remove(force=True)
                logger.info(f"Removed container {container_id}")
            except Exception as e:
                logger.error(f"container.remove(force=True) failed for {container_id}: {e}")
            time.sleep(WAIT_TIME)
            self.container = None
            self.server_port = None
            self.vnc_port = None
            self.chromium_port = None
            self.vlc_port = None
