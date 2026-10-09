"""Read-only detection of a completed Windows setup waiting for its reboot."""
import re
from datetime import datetime, timedelta, timezone


def last_firmware_boot(log):
    opened = re.search(r'Log opened (\S+)', log)
    boots = re.findall(r'^(\d+):(\d+):(\d+\.\d+) EFI:.*VBoxVgaDxe\.efi', log, re.M)
    if not opened or not boots:
        return None
    hours, minutes, seconds = boots[-1]
    return datetime.fromisoformat(opened[1].replace('Z', '+00:00')) + timedelta(
        hours=int(hours), minutes=int(minutes), seconds=float(seconds))


def completed_reboot_request(log):
    return ('Reboot required after setup.exe and PostSysprep commands' in log
            and 'Flushing registry to disk...' in log
            and bool(re.search(r'\[windeploy.exe\] WinDeploy.exe exiting with code \[0x0\]\s*$', log)))


def pending_reboot(directory, name, now=None):
    """Never write or mount the disk; inconsistent/inaccessible records mean no recovery."""
    now = now or datetime.now(timezone.utc)
    try:
        from dissect.hypervisor.disk.vdi import VDI
        from dissect.volume.disk.disk import Disk
        from dissect.ntfs import NTFS
        boot = last_firmware_boot((directory / name / 'Logs' / 'VBox.log').read_text(encoding='utf-8', errors='replace'))
        if boot is None:
            return False
        with VDI(directory / 'system.vdi') as image:
            for partition in Disk(image.open()).partitions:
                try:
                    fs = NTFS(partition.open())
                    record = fs.mft.get('/Windows/Panther/UnattendGC/setupact.log')
                    modified = record.attributes[0x10][0].last_modification_time
                    if not (boot < modified and 300 <= (now - modified).total_seconds() <= 3600):
                        continue
                    with record.open() as stream:
                        stream.seek(0, 2)
                        stream.seek(max(0, stream.tell() - 16384))
                        log = stream.read().decode('utf-8', errors='replace')
                    if completed_reboot_request(log):
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False
