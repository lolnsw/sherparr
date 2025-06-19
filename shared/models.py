from pydantic import BaseModel
from enum import Enum
from datetime import datetime
from typing import Optional, List, Dict


class SyncStatus(str, Enum):
    IDLE = "idle"
    SYNCING = "syncing"
    COMPLETED = "completed"
    FAILED = "failed"


class ZFSSnapshotInfo(BaseModel):
    dataset_name: str
    snapshot_name: str
    created: bool
    error_message: Optional[str] = None


class ShareSyncStats(BaseModel):
    share_name: str
    files_synced: int
    bytes_synced: int
    total_files: int
    duration_seconds: float
    errors: List[str] = []
    post_sync_total_files: int = 0
    post_sync_total_size_bytes: int = 0
    zfs_snapshot: Optional[ZFSSnapshotInfo] = None


class DiskSpaceInfo(BaseModel):
    total_bytes: int
    used_bytes: int
    free_bytes: int
    usage_percent: float


class SyncJobReport(BaseModel):
    total_duration_seconds: float
    total_files_synced: int
    total_bytes_synced: int
    shares_synced: List[ShareSyncStats]
    destination_disk_space: DiskSpaceInfo
    start_time: datetime
    end_time: datetime


class ClientStatus(BaseModel):
    client_id: str
    status: SyncStatus
    message: Optional[str] = None
    last_update: datetime
    progress: Optional[int] = None
    sync_report: Optional[SyncJobReport] = None


class PowerAction(str, Enum):
    ON = "on"
    OFF = "off"


class PowerRequest(BaseModel):
    action: PowerAction


class PowerStatus(BaseModel):
    is_on: bool
    power_consumption: Optional[float] = None
    last_update: datetime