"""Orchestrator CLI. JSON on stdin keeps credentials off process command lines."""
import argparse
import csv
import getpass
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import guest, media, vmware, ssh_bootstrap, credentials
from .profiles import FAMILIES, validate_config

ROOT = Path(__file__).resolve().parents[1]
VBOX = Path(os.environ.get('VBOX_MSI_INSTALL_PATH', r'C:\Program Files\Oracle\VirtualBox')) / 'VBoxManage.exe'
VMRUN = Path(r'C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe')
TERMINAL = {'ready', 'failed', 'needs_attention', 'manual', 'cloned'}


@contextmanager
def reserve_vm(name, job_id):
    """OS locks are released even if Windows or a worker stops unexpectedly."""
    path = ROOT / 'deployments' / ('.reserve-' + name.lower())
    with path.open('a+b') as lock:
        if path.stat().st_size == 0:
            lock.write(b'\0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError('Un deploiement utilise deja ce nom de VM.') from error
        try:
            # Keep the lock inode in place: unlinking it can allow concurrent owners.
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def launch_worker(config, directory):
    """A worker survives a web-server restart; no credential-bearing command line/file."""
    directory = Path(directory)
    private_directory(directory)
    creation = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    with (directory / 'worker.log').open('ab') as output:
        process = subprocess.Popen([sys.executable, '-m', 'automation.engine', 'run', '--directory', str(directory)],
                                   stdin=subprocess.PIPE, stdout=output, stderr=output, cwd=ROOT,
                                   creationflags=creation)
    queued = dict(jobId=directory.name, name=config['name'], state='preparing', progress=0,
                  workerPid=process.pid, message='Demarrage du moteur automatique', updatedAt=Deployment.now())
    (directory / 'status.json').write_text(json.dumps(queued), encoding='utf-8')
    process.stdin.write(json.dumps(config, ensure_ascii=True).encode('utf-8'))
    process.stdin.close()
    return {'jobId': directory.name, 'workerPid': process.pid}


def execute(args, timeout=300, check=True, input=None):
    result = subprocess.run([str(a) for a in args], input=input, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if check and result.returncode:
        # No argv: it may contain a password or an encoded script.
        raise RuntimeError(f'{Path(args[0]).name} : code {result.returncode}. {result.stderr.strip()[-1200:]}')
    return result


def vbox(*args, **kwargs):
    return execute([VBOX, *args], **kwargs)


def machine_values(output):
    values = {}
    for line in output.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            values[key.strip('"')] = value.strip().strip('"')
    return values


def templates():
    path = ROOT / 'templates.json'
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else []


def template_inventory():
    registered = set()
    if VBOX.is_file():
        registered = set(re.findall(r'\{([^}]+)\}', vbox('list', 'vms', check=False).stdout))
    inventory = []
    for template in templates():
        item = {key: value for key, value in template.items() if key != 'passwordHash'}
        if template.get('provider', 'virtualbox') == 'vmware':
            exists = Path(template['sourceVm']).is_file() and VMRUN.is_file()
            reason = 'VMX ou moteur VMware introuvable.'
        else:
            exists = template['sourceVm'] in registered
            reason = 'La VM source n’est plus enregistree dans VirtualBox.'
        item['available'] = exists
        item['availabilityReason'] = '' if exists else reason
        item['requiresPassword'] = template.get('provisioner') != 'fixed' and not template.get('managedCredentials', False)
        inventory.append(item)
    return inventory


def source_path(config):
    name = config.get('isoName', '')
    if not name or Path(name).name != name or '/' in name or '\\' in name or ':' in name:
        raise ValueError('Nom de fichier ISO invalide.')
    path = (ROOT / 'isos' / name).resolve()
    if path.parent != (ROOT / 'isos').resolve():
        raise ValueError('Le support doit se trouver dans le dossier isos.')
    if not path.is_file() or path.stat().st_size < 1024**2:
        raise ValueError('Support absent ou incomplet : importer/telecharger le fichier avant de lancer.')
    if Path(str(path) + '.uploading').exists() or Path(str(path) + '.downloading').exists():
        # Old stale uploads may coexist with a valid ISO; never guess which one is complete.
        raise ValueError('Un transfert est encore present pour ce support. Terminer ou annuler le transfert avant le deploiement.')
    return path


def preflight(raw):
    config = validate_config(raw)
    family, strategy = config['family'], config['strategy']
    family_templates = [t for t in templates() if t['family'] == family]
    available = [t for t in family_templates if t.get('isoName') == config.get('isoName')]
    template = next((t for t in family_templates if t['id'] == config.get('templateId')), None)
    if config.get('templateId') and template is None:
        raise ValueError('Modele introuvable ou incompatible avec la famille selectionnee.')
    if template and template.get('isoName') and template['isoName'] != config.get('isoName'):
        raise ValueError('Le modele correspond a une autre version. Selectionner la version declaree lors de son enregistrement.')
    if available:
        usable_ids = {item['id'] for item in template_inventory() if item['available']}
        available = [item for item in available if item['id'] in usable_ids]
    if strategy == 'template' and template is None and available:
        template = available[0]
        config['templateId'] = template['id']
    if strategy == 'template' and template is None:
        raise ValueError(f'{FAMILIES[family]} : ajouter un modele prepare dans la section Modeles automatiques. Cette image ne dispose pas de parcours ISO autonome compatible.')
    provider = template.get('provider', 'virtualbox') if template else 'virtualbox'
    binary = VMRUN if provider == 'vmware' else VBOX
    if not binary.is_file():
        raise ValueError(f'{binary.name} introuvable. Installer le moteur de virtualisation correspondant.')
    if provider == 'virtualbox':
        registered = vbox('list', 'vms').stdout
        names = re.findall(r'^"(.*)" \{[^}]+\}$', registered, re.M)
        if config['name'] in names:
            raise ValueError('Une VM porte deja ce nom. Choisir un autre nom ; aucune VM existante ne sera effacee.')
        if template:
            info = machine_values(vbox('showvminfo', template['sourceVm'], '--machinereadable').stdout)
            if info.get('VMState') != 'poweroff':
                raise ValueError('Le modele doit etre completement arrete (pas suspendu).')
            config['osType'] = info.get('ostype', config['osType'])
    else:
        if not template or family != 'macos' or template['provisioner'] != 'macos':
            raise ValueError('Le moteur VMware est reserve aux modeles macOS prepares.')
        source = Path(template['sourceVm'])
        if not source.is_file() or source.suffix.lower() != '.vmx':
            raise ValueError('Fichier VMX du modele macOS introuvable.')
        if str(source).lower() in execute([VMRUN, 'list']).stdout.lower():
            raise ValueError('Arreter completement le modele macOS avant son clonage.')
        if config['systemEnv'] != 'gui':
            raise ValueError('Le modele macOS necessite le mode systeme graphique.')
    if strategy != 'template':
        source = source_path(config)
        if source.suffix.lower() != '.iso':
            raise ValueError('Ce fichier doit etre importe comme modele prepare, ou extrait en ISO avant installation.')
        if strategy == 'native':
            detected = machine_values(vbox('unattended', 'detect', '--iso=' + str(source),
                                           '--machine-readable', check=False).stdout)
            if detected.get('IsInstallSupported') not in ('on', 'yes', 'true'):
                if available:
                    config['strategy'] = strategy = 'template'
                    config['templateId'] = available[0]['id']
                    return preflight(config)
                raise ValueError('Cet ISO ne permet pas une installation native autonome avec cette version de VirtualBox. Ajouter un modele prepare pour cette famille.')
            if config['osType'] in ('WindowsXP', 'Windows2003', 'WindowsVista_64', 'Windows7_64'):
                raise ValueError('Ce Windows ancien necessite un modele prepare pour la configuration et sa verification.')
            if family == 'windows' and config['systemEnv'] == 'cli' and not re.search(r'server|Windows20', config['isoName'] + config['osType'], re.I):
                raise ValueError('Windows client ne propose pas de mode sans bureau. Choisir Interface graphique ou Windows Server Core.')
            config['detected'] = detected
        if strategy in ('arch', 'omarchy') and config['disk'] < 40:
            raise ValueError('Arch/Omarchy : allouer au moins 40 Go de disque.')
        if strategy == 'omarchy' and config['systemEnv'] != 'gui':
            raise ValueError('Omarchy est un systeme de bureau. Choisir Interface graphique ou Arch Linux pour un serveur console.')
        # Remastering writes one ISO copy; disks remain dynamically allocated.
        copies_iso = strategy in ('kickstart', 'arch', 'alpine') or (strategy == 'native' and family == 'windows')
        required = source.stat().st_size + 2*1024**3 if copies_iso else 2*1024**3
        if shutil.disk_usage(ROOT).free < required:
            raise ValueError('Espace disque insuffisant pour preparer les supports automatiques.')
    if template:
        if template.get('managedCredentials') and not config.get('templatePassword'):
            try:
                config['templatePassword'] = credentials.load(ROOT, template['id'])
            except (OSError, ValueError, RuntimeError):
                raise ValueError('Identifiant protege du modele inaccessible. Fournir son mot de passe de preparation.')
        if template['provisioner'] in ('linux', 'macos', 'freebsd') and template.get('bootstrapUser', 'root') != 'root':
            raise ValueError('Le compte de preparation doit etre root pour ce type de modele.')
        if template['provisioner'] == 'fixed':
            guest.fixed_template_check(config, template)
        elif not config.get('templatePassword'):
            raise ValueError('Saisir le mot de passe du compte de preparation du modele.')
        if template['provisioner'] == 'freebsd' and config['systemEnv'] != template.get('systemEnv', 'cli'):
            raise ValueError('Choisir le mode bureau/console deja installe dans ce modele FreeBSD.')
    return config, template


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        line = execute(['whoami', '/user', '/fo', 'csv', '/nh']).stdout.strip()
        sid = next(csv.reader([line]))[1]
        execute(['icacls', path, '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'])


class Deployment:
    def __init__(self, config, directory, template=None):
        self.config, self.directory, self.template = config, Path(directory), template
        self.config['deploymentId'] = self.directory.name
        self.status = dict(jobId=self.directory.name, name=config['name'], family=config['family'],
                           strategy=config['strategy'], state='preparing', progress=5,
                           workerPid=os.getpid(), message='Verification des prerequis', startedAt=self.now(), updatedAt=self.now())
        self.lock = threading.RLock()
        self.event = threading.Event()
        self.guest_state = None
        self.server = None
        self.created = False
        self.password_file = self.directory / '.password'

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def clean(self, message):
        for key in ('pass', 'templatePassword'):
            secret = self.config.get(key)
            if secret:
                message = message.replace(secret, '[masque]')
        return message

    def log(self, message):
        with self.lock:
            with (self.directory / 'log.txt').open('a', encoding='utf-8') as file:
                file.write(f'[{datetime.now():%H:%M:%S}] {self.clean(str(message))}\n')

    def update(self, state, message, progress):
        with self.lock:
            self.status.update(state=state, message=self.clean(message), progress=progress, updatedAt=self.now())
            target = self.directory / 'status.json'
            tmp = self.directory / 'status.tmp'
            tmp.write_text(json.dumps(self.status, ensure_ascii=False), encoding='utf-8')
            for attempt in range(10):
                try:
                    tmp.replace(target)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(.05)
            self.log(message)

    def command(self, *args, **kwargs):
        return vbox(*args, **kwargs)

    def callback_server(self):
        owner = self
        token = secrets.token_urlsafe(32)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                request = urlsplit(self.path)
                parts = request.path.split('/')
                if len(parts) != 3 or not secrets.compare_digest(parts[1], token):
                    self.send_error(404)
                    return
                key = parts[2]
                if key in ('configuring', 'ready', 'failed'):
                    with owner.lock:
                        if owner.guest_state not in ('ready', 'failed'):
                            owner.guest_state = key
                            if key == 'configuring':
                                owner.update('configuring', 'Le systeme installe applique et verifie les parametres.', 80)
                            elif key == 'failed':
                                reason = parse_qs(request.query).get('reason', [''])[0]
                                reason = re.sub(r'[^A-Za-z0-9_.:-]', '', reason)[:80]
                                message = 'La configuration dans la VM a echoue.'
                                if reason:
                                    message += ' Detail interne : ' + reason
                                owner.update('failed', message, 80)
                                owner.event.set()
                            else:
                                owner.event.set()
                    content = b'OK'
                elif key in owner.assets:
                    content = owner.assets[key]
                    content = content.encode('utf-8') if isinstance(content, str) else content
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        # VirtualBox NAT maps 10.0.2.2 to the host loopback, without exposing an HTTP listener on the LAN.
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base_url = f'http://10.0.2.2:{self.server.server_port}/{token}'
        self.assets = {'configure.sh': guest.linux_configure(self.config, self.base_url),
                       'stage.sh': guest.linux_stage(self.config, self.base_url),
                       'configure.ps1': guest.windows_configure(self.config, self.base_url)}
        if self.config['strategy'] == 'arch':
            self.assets['install.sh'] = guest.arch_install(self.config, self.base_url)
        if self.config['strategy'] == 'alpine':
            self.assets['install.sh'] = guest.alpine_install(self.config, self.base_url)
            self.assets['vmc.apkovl.tar.gz'] = media.alpine_overlay(self.base_url)
        if self.template and self.template['provisioner'] == 'freebsd':
            self.assets['configure.sh'] = guest.bsd_configure(self.config, self.base_url)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def create(self):
        c = self.config
        self.update('creating', 'Creation du materiel virtuel et du disque.', 15)
        self.command('createvm', '--name', c['name'], '--ostype', c['osType'],
                     '--basefolder', self.directory, '--register')
        self.created = True
        firmware = 'efi' if c['strategy'] in ('omarchy', 'arch') or c['osType'] == 'Windows11_64' else 'bios'
        graphics = 'vboxsvga' if c['family'] == 'windows' else 'vmsvga'
        self.command('modifyvm', c['name'], '--memory', str(int(c['ram']*1024)), '--cpus', str(c['cpu']),
                     '--vram', '128' if c['systemEnv'] == 'gui' else '32', '--graphicscontroller', graphics,
                     '--firmware', firmware, '--ioapic', 'on', '--rtcuseutc', 'off' if c['family'] == 'windows' else 'on',
                     '--boot1', 'disk', '--boot2', 'dvd', '--boot3', 'none', '--boot4', 'none', '--nic1', 'nat',
                     '--nat-localhostreachable1', 'on')
        if c['osType'] == 'Windows11_64':
            self.command('modifyvm', c['name'], '--tpm-type', '2.0')
        if c['family'] == 'omarchy':
            self.command('modifyvm', c['name'], '--accelerate-3d', 'on')
        if c['strategy'] == 'alpine':
            # The virt ISO's initramfs contains virtio_net, but not Intel e1000.
            self.command('modifyvm', c['name'], '--nic-type1', 'virtio')
        self.command('createmedium', 'disk', '--filename', self.directory / 'system.vdi',
                     '--size', str(c['disk']*1024), '--format', 'VDI')
        self.command('storagectl', c['name'], '--name', 'SATA', '--add', 'sata', '--controller', 'IntelAhci', '--hostiocache', 'on')
        self.command('storageattach', c['name'], '--storagectl', 'SATA', '--port', '0', '--device', '0',
                     '--type', 'hdd', '--medium', self.directory / 'system.vdi')
        self.command('storagectl', c['name'], '--name', 'IDE', '--add', 'ide')

    def attach(self, path, device=0):
        self.command('storageattach', self.config['name'], '--storagectl', 'IDE', '--port', '0',
                     '--device', str(device), '--type', 'dvddrive', '--medium', path)

    def prepare_install(self):
        c = self.config
        source = source_path(c)
        self.update('preparing_media', 'Preparation du support et des reponses automatiques.', 30)
        strategy = c['strategy']
        if strategy in ('kickstart', 'arch', 'alpine'):
            prepared = self.directory / 'installation.iso'
            args, files = '', {}
            if strategy == 'kickstart':
                args = 'inst.ks=' + self.base_url + '/ks.cfg inst.noninteractive'
                self.assets['ks.cfg'] = media.kickstart(c, self.base_url)
            elif strategy == 'arch':
                args = 'script=' + self.base_url + '/install.sh'
            else:
                args = ('modules=loop,squashfs,sd-mod,usb-storage,virtio_pci,virtio_net,af_packet '
                        'ip=10.0.2.15::10.0.2.2:255.255.255.0::eth0:none apkovl='
                        + self.base_url + '/vmc.apkovl.tar.gz')
            media.remaster(source, prepared, args, files)
            self.attach(prepared)
        elif strategy == 'omarchy':
            seed = self.directory / 'cidata.iso'
            ssh_bootstrap.prepare(self)
            files = media.omarchy_files(c, self.base_url)
            files['authorized_keys'] = self.ssh_public + '\n'
            media.write_iso(seed, 'cidata', files)
            self.attach(source)
            self.attach(seed, 1)
        elif strategy == 'native':
            if c['family'] == 'windows':
                source = media.windows_autoboot(source, self.directory / 'windows-autoboot.iso')
            self.attach(source)
            self.password_file.write_text(c['pass'], encoding='utf-8')
            opts = media.native_templates(c, self.directory, VBOX.parent / 'UnattendedTemplates', self.base_url)
            if not media.ubuntu_autoinstall(c):
                opts += ['--install-additions']
            if c['family'] == 'windows':
                cmd = "$p=Join-Path $env:TEMP 'vmc.ps1'; (New-Object Net.WebClient).DownloadFile('" + self.base_url + "/configure.ps1',$p); & $p; Remove-Item -LiteralPath $p -Force"
                opts += ['--post-install-command=powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand ' + guest.encoded_powershell(cmd)]
                opts += ['--image-index=' + str(c['imageIndex'])]
                product_key = media.windows_setup_key(c)
                if product_key:
                    opts += ['--key=' + product_key]
            if c['systemEnv'] == 'cli':
                opts += ['--package-selection-adjustment=minimal']
            locale = c['lang'].split('.')[0]
            self.command('unattended', 'install', c['name'], '--iso=' + str(source),
                         '--user=' + c['user'], '--user-password-file=' + str(self.password_file),
                         '--admin-password-file=' + str(self.password_file), '--full-user-name=' + c['fullName'],
                         '--locale=' + locale, '--country=' + locale.split('_')[1],
                         '--time-zone=' + c['timezone'], '--hostname=' + c['hostname'] + '.local',
                         '--auxiliary-base-path=' + str(self.directory / 'unattended-'), *opts)
            self.password_file.unlink(missing_ok=True)
            # Do not remount the original ISO: VirtualBox's generated VISO carries the answers.
        else:
            self.attach(source)

    def clone(self):
        c, t = self.config, self.template
        self.update('creating', 'Clonage complet du modele arrete ; le modele reste intact.', 20)
        self.command('clonevm', t['sourceVm'], '--name', c['name'], '--basefolder', self.directory,
                     '--mode', 'machine', '--register', timeout=3600)
        self.created = True
        self.command('modifyvm', c['name'], '--memory', str(int(c['ram']*1024)), '--cpus', str(c['cpu']), '--nic1', 'nat',
                     '--nat-localhostreachable1', 'on')
        # Cloning keeps the installed disk layout/firmware. Never resize a disk behind its filesystem.
        self.log('Le disque clone conserve la capacite et les partitions du modele.')

    def configure_template(self):
        c, t = self.config, self.template
        if t['provisioner'] == 'fixed':
            self.update('cloned', 'Modele clone et demarre avec ses parametres fixes. Verification interne indisponible pour cet OS.', 100)
            return
        self.update('installing', 'Attente des outils invites du modele.', 50)
        self.password_file.write_text(c['templatePassword'], encoding='utf-8')
        auth = ['--username', t.get('bootstrapUser', 'root'), '--passwordfile', str(self.password_file)]
        end = time.monotonic() + 600
        exe = r'C:\Windows\System32\cmd.exe' if t['provisioner'] == 'windows' else '/bin/sh'
        while time.monotonic() < end:
            result = self.command('guestcontrol', c['name'], 'run', *auth, '--exe', exe,
                                  '--timeout', '10000', '--',
                                  *(['/c', 'exit', '0'] if t['provisioner'] == 'windows' else ['-c', 'id -u']),
                                  check=False, timeout=30)
            if result.returncode == 0:
                break
            time.sleep(5)
        else:
            raise RuntimeError('Outils invites ou compte de preparation inaccessibles dans le modele.')
        windows = t['provisioner'] == 'windows'
        if windows:
            role = '[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)'
            result = self.command('guestcontrol', c['name'], 'run', *auth,
                                  '--exe', r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
                                  '--timeout', '60000', '--wait-stdout', '--wait-stderr', '--',
                                  '-NoProfile', '-Command', role, check=False, timeout=70)
            if result.returncode != 0 or result.stdout.strip() != 'True':
                raise RuntimeError('Le compte de preparation Windows ne dispose pas de droits administrateur eleves via les outils invites. Utiliser un compte de preparation approprie, par exemple le compte administrateur integre active dans le modele.')
        local = self.directory / ('template-configure.ps1' if windows else 'template-configure.sh')
        local.write_text(self.assets['configure.ps1' if windows else 'configure.sh'], encoding='utf-8-sig' if windows else 'utf-8', newline='\n')
        remote = r'C:\Windows\Temp\vmc-configure.ps1' if windows else '/tmp/vmc-configure.sh'
        self.command('guestcontrol', c['name'], 'copyto', *auth, str(local), remote)
        program = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' if windows else '/bin/sh'
        argv = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', remote] if windows else [remote]
        if not windows and t['provisioner'] == 'linux':
            program = '/bin/bash'
        self.command('guestcontrol', c['name'], 'run', *auth, '--exe', program, '--timeout', str(c['timeoutMinutes']*60000),
                     '--wait-stdout', '--wait-stderr', '--', *argv,
                     timeout=c['timeoutMinutes']*60 + 30)
        local.unlink(missing_ok=True)
        self.password_file.unlink(missing_ok=True)

    def wait_ready(self):
        deadline = time.monotonic() + self.config['timeoutMinutes']*60
        recovered_reboot = False
        next_reboot_check = time.monotonic() + 300
        while time.monotonic() < deadline:
            if self.event.wait(5):
                if self.guest_state == 'failed':
                    raise RuntimeError(self.status['message'])
                if self.guest_state == 'ready':
                    self.update('verifying', 'Configuration confirmee par le systeme invite ; finalisation.', 95)
                    self.detach_media()
                    self.update('ready', 'VM installee, configuree et verification interne terminee.', 100)
                    return
            info = machine_values(self.command('showvminfo', self.config['name'], '--machinereadable').stdout)
            if info.get('VMState') in ('aborted', 'poweroff', 'saved'):
                raise RuntimeError('La VM est arretee avant de confirmer sa configuration.')
            if (self.created and not self.template and self.config['family'] == 'windows'
                    and self.config['strategy'] == 'native' and self.guest_state is None
                    and not recovered_reboot and time.monotonic() >= next_reboot_check):
                from .windows_recovery import pending_reboot
                next_reboot_check = time.monotonic() + 30
                if pending_reboot(self.directory, self.config['name']):
                    # The completed setup log and firmware log prove that the requested
                    # reboot never happened. Never reset an installer still working.
                    with self.lock:
                        if self.guest_state is None:
                            self.log('Windows a termine et enregistre sa configuration, mais son redemarrage reste bloque. Relance unique du demarrage.')
                            self.command('controlvm', self.config['name'], 'reset')
                            recovered_reboot = True
        self.update('needs_attention', 'Delai depasse sans confirmation de la VM. Consulter son ecran et ses journaux ; installation non verifiee.', self.status['progress'])

    def detach_media(self):
        info = machine_values(self.command('showvminfo', self.config['name'], '--machinereadable').stdout)
        for key, value in info.items():
            match = re.fullmatch(r'(.+)-(\d+)-(\d+)', key)
            if match and value.lower().endswith(('.iso', '.viso')):
                self.command('storageattach', self.config['name'], '--storagectl', match[1],
                             '--port', match[2], '--device', match[3], '--type', 'dvddrive',
                             '--medium', 'emptydrive', '--forceunmount')

    def run(self):
        private_directory(self.directory)
        reservation = reserve_vm(self.config['name'], self.directory.name)
        reserved = False
        try:
            reservation.__enter__()
            reserved = True
            self.update('preparing', 'Controles termines. Automatisation : ' + self.config['strategy'], 5)
            if self.template and self.template.get('provider') == 'vmware':
                vmware.run(self, execute, VMRUN)
                return True
            if self.config['strategy'] != 'manual':
                self.callback_server()
            if self.template:
                self.clone()
            else:
                self.create()
                self.prepare_install()
            self.update('installing', 'Demarrage de la machine virtuelle et de son installation.', 50)
            self.command('startvm', self.config['name'], '--type', self.config['displayMode'])
            if self.config['family'] == 'windows' and not self.template:
                def press_space_for_cdboot():
                    time.sleep(2)
                    for _ in range(6):
                        try:
                            self.command('controlvm', self.config['name'], 'keyboardputscancode', '39', 'b9')
                        except Exception:
                            pass
                        time.sleep(0.4)
                threading.Thread(target=press_space_for_cdboot, daemon=True).start()
            metadata = {key: self.config[key] for key in ('name', 'user', 'keyboard', 'lang', 'timezone', 'family', 'strategy')}
            (ROOT / 'deployments' / (self.config['name'] + '.meta.json')).write_text(json.dumps(metadata), encoding='utf-8')
            if self.config['strategy'] == 'manual':
                self.update('manual', 'VM creee. Installation manuelle demandee explicitement.', 100)
                return
            if self.template:
                self.configure_template()
                if self.template['provisioner'] == 'fixed':
                    return
            elif self.config['strategy'] == 'omarchy':
                ssh_bootstrap.configure(self)
            self.wait_ready()
        except Exception as error:
            self.update('failed', self.clean(str(error)), self.status['progress'])
            return False
        finally:
            try:
                ssh_bootstrap.cleanup(self)
            except Exception as error:
                self.log('Nettoyage du port temporaire : ' + self.clean(str(error)))
            self.password_file.unlink(missing_ok=True)
            if reserved:
                reservation.__exit__(None, None, None)
            if self.server:
                self.server.shutdown()
                self.server.server_close()
        return self.status['state'] == 'ready'


def register_template(data):
    family = data.get('family')
    if family not in FAMILIES:
        raise ValueError('Famille de modele inconnue.')
    source = data.get('sourceVm', '')
    provider = data.get('provider', 'virtualbox')
    if provider not in ('virtualbox', 'vmware'):
        raise ValueError('Moteur de virtualisation inconnu.')
    if provider == 'virtualbox':
        info = machine_values(vbox('showvminfo', source, '--machinereadable').stdout)
        if info.get('VMState') != 'poweroff':
            raise ValueError('Arreter completement la VM modele avant de l enregistrer.')
    else:
        source = str(Path(source).resolve())
        if not Path(source).is_file() or Path(source).suffix.lower() != '.vmx':
            raise ValueError('Selectionner le fichier VMX du modele macOS arrete.')
        if source.lower() in execute([VMRUN, 'list']).stdout.lower():
            raise ValueError('Arreter le modele VMware avant de l enregistrer.')
        info = {'UUID': source, 'ostype': 'MacOS_64'}
    provisioner = data.get('provisioner', 'linux')
    if provisioner not in ('linux', 'windows', 'freebsd', 'fixed', 'macos'):
        raise ValueError('Methode de configuration inconnue.')
    if (provider == 'vmware') != (provisioner == 'macos') or (provider == 'vmware' and family != 'macos'):
        raise ValueError('Utiliser le moteur VMware et le profil macOS ensemble.')
    if provisioner == 'windows' and family != 'windows':
        raise ValueError('Le profil Windows est reserve aux modeles Windows modernes.')
    if provisioner == 'freebsd' and family != 'freebsd':
        raise ValueError('Le profil FreeBSD est reserve aux modeles FreeBSD.')
    if provisioner == 'linux' and family in ('windows','macos','freebsd','openbsd','netbsd','haiku','reactos','freedos','templeos'):
        raise ValueError('Choisir le profil de ce systeme ou un modele a parametres fixes.')
    template = dict(id=secrets.token_hex(8), name=data.get('name') or source, family=family,
                    sourceVm=info['UUID'], provider=provider, provisioner=provisioner,
                    bootstrapUser=data.get('bootstrapUser') or ('Administrator' if provisioner == 'windows' else 'root'),
                    systemEnv=data.get('systemEnv', 'cli'), osType=info.get('ostype', 'Other_64'))
    template['isoName'] = data.get('isoName', '')
    if provisioner == 'fixed':
        fixed = validate_config(data['fixedConfig'])
        template['fixedConfig'] = {key: fixed[key] for key in ('hostname', 'user', 'fullName', 'lang', 'keyboard', 'timezone', 'systemEnv')}
        template['passwordHash'] = guest.password_hash(fixed)
    records = templates()
    records.append(template)
    path = ROOT / 'templates.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)
    return {k: v for k, v in template.items() if k != 'passwordHash'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('check', 'run', 'launch', 'templates', 'register'))
    parser.add_argument('--directory')
    args = parser.parse_args()
    try:
        if args.action == 'templates':
            result = template_inventory()
        else:
            data = json.load(sys.stdin)
            if args.action == 'register':
                result = register_template(data)
            else:
                config, template = preflight(data)
                if args.action == 'launch':
                    print(json.dumps(launch_worker(config, args.directory)))
                    return 0
                if args.action == 'run':
                    deployment = Deployment(config, args.directory, template)
                    deployment.run()
                    return 1 if deployment.status['state'] in ('failed', 'needs_attention') else 0
                result = dict(ok=True, family=config['family'], strategy=config['strategy'],
                              templateId=config.get('templateId'), message='Prerequis valides. Installation et configuration automatiques.' if config['strategy'] != 'manual' else 'Mode manuel choisi.')
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as error:
        message = str(error)
        if isinstance(locals().get('data'), dict):
            for key in ('pass', 'templatePassword'):
                if data.get(key):
                    message = message.replace(data[key], '[masque]')
        print(json.dumps(dict(ok=False, error=message), ensure_ascii=True))
        if args.action == 'run' and args.directory:
            directory = Path(args.directory)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'status.json').write_text(json.dumps(dict(state='failed', progress=0, message=message)), encoding='utf-8')
        return 1


if __name__ == '__main__':
    sys.exit(main())
