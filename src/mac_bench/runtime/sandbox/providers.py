"""Resolve a task's cloud configuration once and construct its backend."""

from dataclasses import dataclass, replace
import os

from dotenv import load_dotenv
from mac_bench.runtime.paths import repository_root


@dataclass(frozen=True)
class BackendConfig:
    provider: str
    role: str
    cpu: float
    memory_mib: int
    disk_gib: int
    lifetime_seconds: int
    endpoint: str = ""
    target: str = ""
    app_name: str = "mac-bench"
    idle_timeout_seconds: int = 7200

    def workspace(self):
        return replace(
            self,
            role="workspace",
            cpu=1 if self.provider == "daytona" else 0.5,
            memory_mib=1024 if self.provider == "daytona" else 512,
            disk_gib=3,
            lifetime_seconds=86400,
        )


def provider_name(domain):
    load_dotenv(repository_root() / ".env", override=False)
    if domain not in {"business", "software"}:
        raise ValueError("Domain must be business or software")
    variable = "MAC_BENCH_" + domain.upper() + "_PROVIDER"
    provider = os.environ.get(variable, "modal")
    if provider not in {"modal", "daytona"}:
        raise ValueError(variable + " must be modal or daytona")
    return provider


def resolve_config(domain):
    provider = provider_name(domain)
    config = BackendConfig(
        provider=provider,
        role=domain,
        cpu=float(os.environ.get("MAC_BENCH_CPUS", "4")) if domain == "business" else 4,
        memory_mib=int(os.environ.get("MAC_BENCH_MEMORY_MIB", "8192"))
        if domain == "business"
        else 8192,
        disk_gib=int(os.environ.get("MAC_BENCH_DAYTONA_DISK_GIB", "10"))
        if provider == "daytona"
        else 0,
        lifetime_seconds=7200,
        endpoint=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api")
        if provider == "daytona"
        else "",
        target=os.environ.get("DAYTONA_TARGET", "us") if provider == "daytona" else "",
    )
    if config.cpu <= 0 or config.memory_mib <= 0:
        raise ValueError("Sandbox CPU and memory must be positive")
    if provider == "daytona" and (
        int(config.cpu) != config.cpu or config.memory_mib % 1024 or config.disk_gib < 1
    ):
        raise ValueError("Daytona requires whole CPU cores and GiB allocations")
    return config


def create_backend(config):
    if config.provider == "modal":
        from .modal import ModalSandboxBackend

        backend = ModalSandboxBackend(
            app_name=config.app_name,
            vm_runtime=config.role == "business",
            timeout=config.lifetime_seconds,
            cpu=config.cpu,
            memory_mib=config.memory_mib,
        )
    elif config.provider == "daytona":
        from .daytona import DaytonaSandboxBackend

        backend = DaytonaSandboxBackend(
            docker=config.role == "business",
            idle_timeout=config.idle_timeout_seconds,
            cpu=config.cpu,
            memory_mib=config.memory_mib,
            disk_gib=config.disk_gib,
            endpoint=config.endpoint,
            target=config.target,
        )
    else:
        raise ValueError("Unknown sandbox provider: " + config.provider)
    backend.config = config
    return backend
