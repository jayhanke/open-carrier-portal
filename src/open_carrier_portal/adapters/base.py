from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from open_carrier_portal.models import (
    Alarm,
    CapabilitySet,
    Device,
    Interface,
    MetricSeries,
    Page,
)


class MonitoringAdapter(ABC):
    @abstractmethod
    async def health_check(self) -> None:
        """Raise a typed connector error when the upstream is unavailable."""

    @abstractmethod
    async def capabilities(self) -> CapabilitySet:
        """Return capabilities verified for the configured upstream."""

    @abstractmethod
    async def list_devices(self, *, page: int, page_size: int) -> Page[Device]:
        """List visible devices."""

    @abstractmethod
    async def get_device(self, device_id: str) -> Device:
        """Get one device by its OCP identifier."""

    @abstractmethod
    async def list_interfaces(self, *, page: int, page_size: int) -> Page[Interface]:
        """List visible interfaces."""

    @abstractmethod
    async def get_interface(self, interface_id: str) -> Interface:
        """Get one interface by its OCP identifier."""

    @abstractmethod
    async def list_alarms(self, *, page: int, page_size: int) -> Page[Alarm]:
        """List current alarms."""

    @abstractmethod
    async def query_metrics(
        self,
        *,
        resource_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> MetricSeries:
        """Return numeric points when the upstream supports a verified source."""
