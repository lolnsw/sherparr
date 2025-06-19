import asyncio
import subprocess
import os
import sys
import signal
import re
import shutil
from datetime import datetime
from typing import Optional, List
from pathlib import Path
import httpx
import logging
from logging.handlers import RotatingFileHandler

from shared.models import ClientStatus, SyncStatus, SyncJobReport, ShareSyncStats, DiskSpaceInfo, ZFSSnapshotInfo


def setup_logging():
    """Setup logging with rotation to /mnt/user/logs/sherparr"""
    log_dir = Path("/mnt/user/logs/sherparr")
    log_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = log_dir / "sherparr-client.log"
    
    # Create rotating file handler (1MB max, keep 10 files)
    file_handler = RotatingFileHandler(
        log_file, 
        maxBytes=1024*1024,  # 1MB
        backupCount=10
    )
    
    # Create console handler
    console_handler = logging.StreamHandler()
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    # Setup root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    return logging.getLogger(__name__)


# Initialize logging
logger = setup_logging()


class SherparrClient:
    def __init__(self):
        self.client_id = os.getenv("CLIENT_ID", "unraid-client")
        self.server_url = os.getenv("SERVER_URL", "http://sherparr-server:8000")
        self.remote_base = os.getenv("REMOTE_BASE", "/mnt/remotes")
        self.dest_base = os.getenv("DEST_BASE", "/mnt/users")
        self.rsync_options = os.getenv("RSYNC_OPTIONS", "-avz --progress --delete --stats --verbose --itemize-changes")
        self.shutdown_delay = int(os.getenv("SHUTDOWN_DELAY", "1800"))  # 30 minutes
        self.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
        self.zfs_pool = os.getenv("ZFS_POOL", "tank")
        self.zfs_parent_dataset = os.getenv("ZFS_PARENT_DATASET", "users")
        self.create_snapshots = os.getenv("CREATE_SNAPSHOTS", "true").lower() == "true"
        
        self.rsync_process: Optional[subprocess.Popen] = None
        self.should_shutdown = True
        self.job_start_time: Optional[datetime] = None

    async def send_status_update(self, status: SyncStatus, message: str = None, progress: int = None, sync_report: SyncJobReport = None):
        client_status = ClientStatus(
            client_id=self.client_id,
            status=status,
            message=message,
            last_update=datetime.now(),
            progress=progress,
            sync_report=sync_report
        )
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.server_url}/client/status",
                    json=client_status.model_dump(mode='json'),
                    timeout=10.0
                )
                response.raise_for_status()
                logger.info(f"Status update sent: {status}")
        except Exception as e:
            logger.error(f"Failed to send status update: {e}")

    def discover_media_shares(self) -> List[str]:
        """Discover all shares in /mnt/remote that start with 'media'"""
        try:
            remote_path = Path(self.remote_base)
            if not remote_path.exists():
                logger.error(f"Remote base path {self.remote_base} does not exist")
                return []
            
            media_shares = []
            for item in remote_path.iterdir():
                if item.is_dir() and item.name.startswith("media"):
                    media_shares.append(item.name)
                    logger.info(f"Found media share: {item.name}")
            
            return sorted(media_shares)
        except Exception as e:
            logger.error(f"Failed to discover media shares: {e}")
            return []

    def get_disk_space_info(self, path: str) -> DiskSpaceInfo:
        """Get disk space information for a path"""
        try:
            total, used, free = shutil.disk_usage(path)
            usage_percent = (used / total) * 100 if total > 0 else 0
            
            return DiskSpaceInfo(
                total_bytes=total,
                used_bytes=used,
                free_bytes=free,
                usage_percent=round(usage_percent, 2)
            )
        except Exception as e:
            logger.error(f"Failed to get disk space info for {path}: {e}")
            return DiskSpaceInfo(
                total_bytes=0,
                used_bytes=0,
                free_bytes=0,
                usage_percent=0.0
            )

    def parse_rsync_stats(self, output: str) -> dict:
        """Parse rsync --stats output"""
        stats = {
            'files_transferred': 0,
            'total_size': 0,
            'total_files': 0,
            'errors': []
        }
        
        try:
            # Parse rsync stats output
            if 'Number of files transferred:' in output:
                match = re.search(r'Number of files transferred: (\d+)', output)
                if match:
                    stats['files_transferred'] = int(match.group(1))
            
            if 'Total transferred file size:' in output:
                match = re.search(r'Total transferred file size: ([\d,]+)', output)
                if match:
                    stats['total_size'] = int(match.group(1).replace(',', ''))
            
            if 'Number of regular files transferred:' in output:
                match = re.search(r'Number of regular files transferred: (\d+)', output)
                if match:
                    stats['total_files'] = int(match.group(1))
                    
        except Exception as e:
            logger.error(f"Failed to parse rsync stats: {e}")
            stats['errors'].append(f"Stats parsing error: {e}")
        
        return stats

    def get_share_total_stats(self, path: str) -> tuple[int, int]:
        """Get total file count and size for entire share"""
        total_files = 0
        total_size = 0
        
        try:
            base_path = Path(path)
            if not base_path.exists():
                logger.warning(f"Path {path} does not exist")
                return 0, 0
            
            # Walk through all files in the share
            for file_path in base_path.rglob('*'):
                if file_path.is_file():
                    try:
                        total_files += 1
                        total_size += file_path.stat().st_size
                    except (OSError, PermissionError) as e:
                        logger.warning(f"Could not access file {file_path}: {e}")
                        continue
            
            logger.info(f"Share total: {total_files} files, {total_size/1024/1024:.1f}MB")
            return total_files, total_size
            
        except Exception as e:
            logger.error(f"Failed to analyze share contents in {path}: {e}")
            return 0, 0

    def create_zfs_snapshot(self, share_name: str) -> ZFSSnapshotInfo:
        """Create a ZFS snapshot for the synced share"""
        if not self.create_snapshots:
            return ZFSSnapshotInfo(
                dataset_name=f"{self.zfs_pool}/{self.zfs_parent_dataset}/{share_name}",
                snapshot_name="",
                created=False,
                error_message="Snapshot creation disabled"
            )
        
        # Generate snapshot name with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dataset_name = f"{self.zfs_pool}/{self.zfs_parent_dataset}/{share_name}"
        snapshot_name = f"{dataset_name}@sherparr_sync_{timestamp}"
        
        try:
            if self.dry_run:
                logger.info(f"DRY RUN: Would create ZFS snapshot: {snapshot_name}")
                return ZFSSnapshotInfo(
                    dataset_name=dataset_name,
                    snapshot_name=snapshot_name,
                    created=True,
                    error_message="Dry run mode"
                )
            
            logger.info(f"Creating ZFS snapshot: {snapshot_name}")
            
            # Create the snapshot
            result = subprocess.run(
                ["zfs", "snapshot", snapshot_name],
                capture_output=True,
                text=True,
                check=True
            )
            
            logger.info(f"ZFS snapshot created successfully: {snapshot_name}")
            return ZFSSnapshotInfo(
                dataset_name=dataset_name,
                snapshot_name=snapshot_name,
                created=True
            )
            
        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to create ZFS snapshot {snapshot_name}: {e.stderr}"
            logger.error(error_msg)
            return ZFSSnapshotInfo(
                dataset_name=dataset_name,
                snapshot_name=snapshot_name,
                created=False,
                error_message=error_msg
            )
        except Exception as e:
            error_msg = f"ZFS snapshot creation failed for {share_name}: {e}"
            logger.error(error_msg)
            return ZFSSnapshotInfo(
                dataset_name=dataset_name,
                snapshot_name=snapshot_name,
                created=False,
                error_message=error_msg
            )

    async def sync_share(self, share_name: str) -> ShareSyncStats:
        """Sync a single share and return statistics"""
        source_path = f"{self.remote_base}/{share_name}/"
        # Remove _remote suffix from destination path
        dest_share_name = share_name.replace("_remote", "")
        dest_path = f"{self.dest_base}/{dest_share_name}/"
        
        logger.info(f"Starting sync: {source_path} -> {dest_path}")
        
        # Ensure destination exists
        Path(dest_path).mkdir(parents=True, exist_ok=True)
        
        start_time = datetime.now()
        errors = []
        
        try:
            cmd = f"rsync {self.rsync_options} {source_path} {dest_path}"
            
            if self.dry_run:
                # Add --dry-run to rsync command for simulation
                cmd = f"rsync --dry-run {self.rsync_options} {source_path} {dest_path}"
                logger.info(f"DRY RUN: Executing: {cmd}")
            else:
                logger.info(f"Executing: {cmd}")
            
            process = subprocess.Popen(
                cmd.split(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            
            # Stream output in real-time while process runs
            stdout_lines = []
            stderr_lines = []
            
            while True:
                # Check if process has finished
                if process.poll() is not None:
                    # Get any remaining output
                    remaining_stdout, remaining_stderr = process.communicate()
                    if remaining_stdout:
                        for line in remaining_stdout.splitlines():
                            logger.info(f"rsync: {line}")
                            stdout_lines.append(line)
                    if remaining_stderr:
                        for line in remaining_stderr.splitlines():
                            logger.error(f"rsync error: {line}")
                            stderr_lines.append(line)
                    break
                
                # Read stdout
                if process.stdout and process.stdout.readable():
                    try:
                        line = process.stdout.readline()
                        if line:
                            line = line.strip()
                            if line:
                                logger.info(f"rsync: {line}")
                                stdout_lines.append(line)
                    except:
                        pass
                
                # Read stderr
                if process.stderr and process.stderr.readable():
                    try:
                        line = process.stderr.readline()
                        if line:
                            line = line.strip()
                            if line:
                                logger.error(f"rsync error: {line}")
                                stderr_lines.append(line)
                    except:
                        pass
                
                # Small delay to prevent excessive CPU usage
                await asyncio.sleep(0.1)
            
            stdout = '\n'.join(stdout_lines)
            stderr = '\n'.join(stderr_lines)
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            if process.returncode == 0:
                stats = self.parse_rsync_stats(stdout)
                logger.info(f"Share {share_name} synced successfully in {duration:.1f}s")
                
                # Get post-sync share totals
                logger.info(f"Analyzing post-sync totals for {share_name}")
                post_sync_files, post_sync_size = self.get_share_total_stats(dest_path.rstrip('/'))
                
                # Create ZFS snapshot after successful sync
                snapshot_info = self.create_zfs_snapshot(dest_share_name)
                
                return ShareSyncStats(
                    share_name=dest_share_name,  # Use cleaned name
                    files_synced=stats['files_transferred'],
                    bytes_synced=stats['total_size'],
                    total_files=stats['total_files'],
                    duration_seconds=duration,
                    errors=stats['errors'],
                    post_sync_total_files=post_sync_files,
                    post_sync_total_size_bytes=post_sync_size,
                    zfs_snapshot=snapshot_info
                )
            else:
                error_msg = f"Rsync failed for {share_name}: {stderr}"
                logger.error(error_msg)
                errors.append(error_msg)
                
                # Even on failure, get what's there
                post_sync_files, post_sync_size = self.get_share_total_stats(dest_path.rstrip('/'))
                
                # Don't create snapshot on sync failure
                snapshot_info = ZFSSnapshotInfo(
                    dataset_name=f"{self.zfs_pool}/{self.zfs_parent_dataset}/{dest_share_name}",
                    snapshot_name="",
                    created=False,
                    error_message="Sync failed - snapshot not created"
                )
                
                return ShareSyncStats(
                    share_name=dest_share_name,  # Use cleaned name
                    files_synced=0,
                    bytes_synced=0,
                    total_files=0,
                    duration_seconds=duration,
                    errors=errors,
                    post_sync_total_files=post_sync_files,
                    post_sync_total_size_bytes=post_sync_size,
                    zfs_snapshot=snapshot_info
                )
                
        except Exception as e:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            error_msg = f"Sync execution failed for {share_name}: {e}"
            logger.error(error_msg)
            
            # Even on exception, get what's there
            post_sync_files, post_sync_size = self.get_share_total_stats(dest_path.rstrip('/'))
            
            # Don't create snapshot on exception
            snapshot_info = ZFSSnapshotInfo(
                dataset_name=f"{self.zfs_pool}/{self.zfs_parent_dataset}/{dest_share_name}",
                snapshot_name="",
                created=False,
                error_message="Sync exception - snapshot not created"
            )
            
            return ShareSyncStats(
                share_name=dest_share_name,  # Use cleaned name
                files_synced=0,
                bytes_synced=0,
                total_files=0,
                duration_seconds=duration,
                errors=[error_msg],
                post_sync_total_files=post_sync_files,
                post_sync_total_size_bytes=post_sync_size,
                zfs_snapshot=snapshot_info
            )

    async def run_sync_job(self) -> bool:
        """Run the complete sync job for all media shares"""
        self.job_start_time = datetime.now()
        await self.send_status_update(SyncStatus.SYNCING, "Discovering media shares")
        
        # Discover shares
        media_shares = self.discover_media_shares()
        if not media_shares:
            logger.warning("No media shares found")
            await self.send_status_update(SyncStatus.FAILED, "No media shares found to sync")
            return False
        
        logger.info(f"Found {len(media_shares)} media shares to sync: {media_shares}")
        
        # Sync each share
        share_stats = []
        total_files = 0
        total_bytes = 0
        
        for i, share in enumerate(media_shares):
            await self.send_status_update(
                SyncStatus.SYNCING, 
                f"Syncing share {i+1}/{len(media_shares)}: {share}"
            )
            
            stats = await self.sync_share(share)
            share_stats.append(stats)
            total_files += stats.files_synced
            total_bytes += stats.bytes_synced
        
        # Get final disk space info
        disk_info = self.get_disk_space_info(self.dest_base)
        
        job_end_time = datetime.now()
        total_duration = (job_end_time - self.job_start_time).total_seconds()
        
        # Create comprehensive report
        sync_report = SyncJobReport(
            total_duration_seconds=total_duration,
            total_files_synced=total_files,
            total_bytes_synced=total_bytes,
            shares_synced=share_stats,
            destination_disk_space=disk_info,
            start_time=self.job_start_time,
            end_time=job_end_time
        )
        
        # Check if any shares failed
        failed_shares = [s for s in share_stats if s.errors]
        success = len(failed_shares) == 0
        
        status_msg = f"Synced {len(media_shares)} shares, {total_files} files, {total_bytes/1024/1024:.1f}MB in {total_duration:.1f}s"
        if failed_shares:
            status_msg += f" ({len(failed_shares)} shares had errors)"
        
        await self.send_status_update(
            SyncStatus.COMPLETED if success else SyncStatus.FAILED,
            status_msg,
            sync_report=sync_report
        )
        
        return success

    async def shutdown_unraid(self):
        if self.dry_run:
            logger.info("DRY RUN: Would shutdown Unraid now")
            return
            
        logger.info("Shutting down Unraid...")
        try:
            subprocess.run(["shutdown", "-h", "now"], check=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to shutdown: {e}")
        except Exception as e:
            logger.error(f"Shutdown command failed: {e}")

    def signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, stopping gracefully...")
        self.should_shutdown = False
        if self.rsync_process:
            self.rsync_process.terminate()
        sys.exit(0)

    async def run(self):
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)
        
        logger.info("=" * 60)
        logger.info(f"Sherparr Client starting (ID: {self.client_id})")
        logger.info(f"Remote base: {self.remote_base}")
        logger.info(f"Destination base: {self.dest_base}")
        logger.info(f"Dry run mode: {self.dry_run}")
        logger.info(f"ZFS snapshots: {self.create_snapshots}")
        logger.info("=" * 60)
        
        # Initial status
        await self.send_status_update(SyncStatus.IDLE, "Client started")
        
        # Run sync job
        success = await self.run_sync_job()
        
        if success and self.should_shutdown:
            # Wait for shutdown delay
            logger.info("=" * 60)
            logger.info(f"SYNC COMPLETED SUCCESSFULLY")
            logger.info(f"Waiting {self.shutdown_delay} seconds before shutdown...")
            logger.info("=" * 60)
            await asyncio.sleep(self.shutdown_delay)
            
            if self.should_shutdown:
                await self.shutdown_unraid()
        else:
            logger.warning("=" * 60)
            logger.warning("SYNC FAILED OR SHUTDOWN CANCELLED")
            logger.warning("Staying online for manual intervention")
            logger.warning("=" * 60)
            await self.send_status_update(SyncStatus.IDLE, "Waiting for manual intervention")
            
            # Keep alive
            while True:
                await asyncio.sleep(60)
                await self.send_status_update(SyncStatus.IDLE, "Waiting for manual intervention")


async def main():
    client = SherparrClient()
    await client.run()


if __name__ == "__main__":
    asyncio.run(main())