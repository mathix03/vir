"""aria2 worker; preserve the server's progress and cancellation protocol."""
import json
import hashlib
import math
import re
import secrets
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen


def finalize_download(partial, dest, options, progress, cancel):
    """Publish only after hashing, optional publisher verification and ZIP CRC checks."""
    progress.write_text('VERIFYING', encoding='utf-8')
    expected = options.get('sha256', '')
    if expected and not re.fullmatch(r'[a-fA-F0-9]{64}', expected):
        raise ValueError('Somme SHA-256 invalide.')
    archive_sha1 = options.get('sha1', '')
    if archive_sha1 and not re.fullmatch(r'[a-fA-F0-9]{40}', archive_sha1):
        raise ValueError('Empreinte SHA-1 de l archive invalide.')
    digest = hashlib.sha256()
    archive_digest = hashlib.sha1() if archive_sha1 else None
    with partial.open('rb') as stream:
        prefix = stream.read(4096)
        if prefix.lstrip().lower().startswith((b'<!doctype html', b'<html')):
            raise ValueError('Le serveur a renvoye une page HTML au lieu du fichier.')
        stream.seek(0)
        while chunk := stream.read(1024 * 1024):
            if cancel.exists():
                raise InterruptedError('Annulation pendant la verification.')
            digest.update(chunk)
            if archive_digest:
                archive_digest.update(chunk)
    if not partial.stat().st_size:
        raise ValueError('Le fichier telecharge est vide.')
    actual = digest.hexdigest()
    if expected and actual != expected.lower():
        raise ValueError('SHA-256 incorrect : le fichier ne correspond pas a la source attendue.')
    if archive_digest and archive_digest.hexdigest() != archive_sha1.lower():
        raise ValueError('SHA-1 incorrect : le fichier ne correspond pas a l archive attendue.')
    extracted = Path(str(dest) + '.extracting')
    try:
        if options.get('archive') == 'zip':
            progress.write_text('EXTRACTING', encoding='utf-8')
            with zipfile.ZipFile(partial) as archive:
                images = [item for item in archive.infolist() if not item.is_dir() and item.filename.lower().endswith('.iso')]
                if len(images) != 1:
                    raise ValueError('Le ZIP doit contenir exactement un fichier ISO.')
                if images[0].file_size > 32 * 1024**3:
                    raise ValueError('ISO extrait trop volumineux (maximum 32 Go).')
                with archive.open(images[0]) as source, extracted.open('wb') as output:
                    while chunk := source.read(1024 * 1024):
                        if cancel.exists():
                            raise InterruptedError('Annulation pendant l’extraction.')
                        output.write(chunk)
            extracted.replace(dest)
            partial.unlink()
        else:
            partial.replace(dest)
    finally:
        extracted.unlink(missing_ok=True)
    Path(str(dest) + '.verification.json').write_text(json.dumps({
        'url': options.get('url'), 'downloadSha256': actual,
        'expectedSha256': expected or None, 'publisherChecksumVerified': bool(expected),
        'expectedArchiveSha1': archive_sha1 or None, 'archiveChecksumVerified': bool(archive_sha1),
        'archiveCrcVerified': options.get('archive') == 'zip',
        'completedAt': time.time(), 'sizeBytes': dest.stat().st_size,
    }), encoding='utf-8')


def download(url, destination, executable, options=None):
    options = json.loads(options) if isinstance(options, str) else dict(options or {})
    options['url'] = url
    dest = Path(destination)
    progress = Path(str(dest) + '.progress')
    cancel = Path(str(dest) + '.cancel')
    marker = Path(str(dest) + '.downloading')
    partial = Path(str(dest) + '.tmp')
    process = None
    status = 'ERROR'
    smoothed_speed = 0.0
    previous_sample = time.monotonic()
    first_transfer = None
    try:
        Path(str(dest) + '.error').unlink(missing_ok=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        token = secrets.token_hex(24)
        args = [executable, '--no-conf', '--continue=true', '--split=8',
                '--max-connection-per-server=8', '--min-split-size=1M',
                '--file-allocation=none', '--auto-file-renaming=false', '--auto-save-interval=1',
                '--allow-overwrite=false', '--max-tries=5', '--retry-wait=3',
                '--connect-timeout=30', '--timeout=60', '--enable-rpc=true',
                '--rpc-listen-all=false', f'--rpc-listen-port={port}',
                f'--rpc-secret={token}', '--summary-interval=0',
                f'--dir={dest.parent}', f'--out={partial.name}', url]
        with Path(str(dest) + '.aria2.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(args, stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            while process.poll() is None:
                if cancel.exists():
                    status = 'CANCELLED'
                    break
                try:
                    payload = json.dumps({'jsonrpc': '2.0', 'id': 'progress',
                                          'method': 'aria2.tellActive',
                                          'params': ['token:' + token]}).encode()
                    request = Request(f'http://127.0.0.1:{port}/jsonrpc', payload,
                                      {'Content-Type': 'application/json'})
                    with urlopen(request, timeout=2) as response:
                        active = json.load(response)['result']
                    if active:
                        item = active[0]
                        now = time.monotonic()
                        speed = int(item.get('downloadSpeed', 0))
                        if speed > 0 and first_transfer is None:
                            first_transfer = now
                        alpha = 1 - math.exp(-(now - previous_sample) / 5)
                        smoothed_speed = (smoothed_speed + alpha * (speed - smoothed_speed)
                                          if smoothed_speed else float(speed))
                        previous_sample = now
                        age = now - first_transfer if first_transfer is not None else 0
                        displayed_speed = round(smoothed_speed) if speed > 0 else 0
                        progress.write_text(
                            f"{item['completedLength']}|{item['totalLength']}|{displayed_speed}|"
                            f"{item.get('connections', 0)}|{age:.1f}", encoding='utf-8')
                    else:
                        payload = json.dumps({'jsonrpc': '2.0', 'id': 'result',
                                              'method': 'aria2.tellStopped',
                                              'params': ['token:' + token, 0, 1]}).encode()
                        request = Request(f'http://127.0.0.1:{port}/jsonrpc', payload,
                                          {'Content-Type': 'application/json'})
                        with urlopen(request, timeout=2) as response:
                            stopped = json.load(response)['result']
                        if stopped:
                            if stopped[0]['status'] == 'complete':
                                status = 'TRANSFERRED'
                            else:
                                Path(str(dest) + '.error').write_text(
                                    stopped[0].get('errorMessage', 'aria2 download failed'), encoding='utf-8')
                            break
                except (OSError, ValueError, KeyError):
                    pass  # RPC starts after the process; retry on the next tick.
                # Completed/error results also terminate RPC downloads via stop.
                time.sleep(0.25)
            if status == 'TRANSFERRED' or (status != 'CANCELLED' and process.poll() == 0 and partial.exists() and not Path(str(partial) + '.aria2').exists()):
                status = 'ERROR'
                try:
                    finalize_download(partial, dest, options, progress, cancel)
                except InterruptedError:
                    raise
                except Exception:
                    # Keep rejected media for diagnosis; a retry starts a fresh transfer.
                    if partial.exists():
                        rejected = Path(str(dest) + '.rejected-' + secrets.token_hex(4))
                        partial.replace(rejected)
                    raise
                status = 'DONE'
    except InterruptedError:
        status = 'CANCELLED'
    except Exception as exc:
        Path(str(dest) + '.error').write_text(str(exc), encoding='utf-8')
    finally:
        # Publish completion immediately, before aria2's graceful shutdown delay.
        if status == 'DONE':
            progress.write_text(status, encoding='utf-8')
        if process is not None and process.poll() is None:
            try:
                if status == 'CANCELLED':
                    payload = json.dumps({'jsonrpc': '2.0', 'id': 'pause',
                                          'method': 'aria2.forcePauseAll',
                                          'params': ['token:' + token]}).encode()
                    with urlopen(Request(f'http://127.0.0.1:{port}/jsonrpc', payload,
                                         {'Content-Type': 'application/json'}), timeout=2):
                        pass
                payload = json.dumps({'jsonrpc': '2.0', 'id': 'shutdown',
                                      'method': 'aria2.shutdown',
                                      'params': ['token:' + token]}).encode()
                with urlopen(Request(f'http://127.0.0.1:{port}/jsonrpc', payload,
                                     {'Content-Type': 'application/json'}), timeout=2):
                    pass
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()
        progress.write_text(status, encoding='utf-8')
        if status == 'ERROR' and not Path(str(dest) + '.error').exists():
            Path(str(dest) + '.error').write_text('aria2 s’est arrete avant de terminer. Consultez le fichier .aria2.log.', encoding='utf-8')
        marker.unlink(missing_ok=True)
        cancel.unlink(missing_ok=True)
    return status


if __name__ == '__main__':
    sys.exit(0 if download(*sys.argv[1:]) == 'DONE' else 1)
