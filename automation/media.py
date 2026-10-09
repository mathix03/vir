"""Supports de reponse et adaptation non destructive des ISO d'installation."""
import io
import json
import re
import shlex
import shutil
import tarfile
from pathlib import Path

import pycdlib

from .guest import password_hash
from .profiles import KEYBOARDS


def windows_autoboot(source, destination):
    """Use Microsoft's included no-prompt EFI loader in a private ISO copy."""
    iso = pycdlib.PyCdlib()
    iso.open(str(source))
    try:
        payload, original = io.BytesIO(), io.BytesIO()
        try:
            iso.get_file_from_iso_fp(payload, udf_path='/efi/microsoft/boot/efisys_noprompt.bin')
            iso.get_file_from_iso_fp(original, udf_path='/efi/microsoft/boot/efisys.bin')
        except pycdlib.pycdlibexception.PyCdlibException:
            return source
        catalog = iso.eltorito_boot_catalog
        entries = [e for s in catalog.sections if s.platform_id == 0xef for e in s.section_entries] if catalog else []
        if not entries or len(payload.getvalue()) != len(original.getvalue()):
            return source
        offset = entries[0].load_rba * 2048
        with open(source, 'rb') as stream:
            stream.seek(offset)
            if stream.read(len(original.getvalue())) != original.getvalue():
                raise ValueError('Le support EFI Windows ne correspond pas au chargeur attendu.')
    finally:
        iso.close()
    shutil.copyfile(source, destination)
    with open(destination, 'r+b') as stream:
        stream.seek(offset)
        stream.write(payload.getvalue())
    return destination


def windows_setup_key(config):
    if config.get('productKey'):
        return config['productKey']
    edition = config.get('detected', {}).get('ImageIndex' + str(config['imageIndex']), '')
    # A Pro installation key must never be sent to Home, Education or Server.
    if re.match(r'^Windows\s+1[01]\s+(?:Professionnel|Pro)\s*\(', edition, re.I):
        return 'VK7JG-NPHTM-C97JM-9MPGT-3V66T'
    return None


def write_iso(path, label, files):
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, rock_ridge='1.09', joliet=3, vol_ident=label)
    try:
        for index, (name, content) in enumerate(files.items()):
            data = content.encode('utf-8') if isinstance(content, str) else content
            iso.add_fp(io.BytesIO(data), len(data), f'/FILE{index:04d}.DAT;1',
                       rr_name=name, joliet_path='/' + name)
        iso.write(str(path))
    finally:
        iso.close()


def kickstart(config, base_url):
    console, layout, variant, _ = KEYBOARDS[config['keyboard']]
    # Only DVD installers reach this profile. Live/Atomic variants require a template.
    return f'''# VM Configurator - installation sur le disque neuf de cette VM
cdrom
text
lang {config['lang']}
keyboard --vckeymap={console} --xlayouts='{layout}{' (' + variant + ')' if variant else ''}'
timezone {config['timezone']} --utc
network --bootproto=dhcp --device=link --activate --onboot=on --hostname={config['hostname']}
rootpw --lock
user --name={config['user']} --password={password_hash(config)} --iscrypted --groups=wheel
firewall --enabled --service=ssh
selinux --enforcing
ignoredisk --only-use=sda
zerombr
clearpart --all --initlabel --drives=sda
autopart
bootloader --boot-drive=sda
firstboot --disable
reboot --eject
%packages
@core
kernel
sudo
curl
openssh-server
%end
%post --erroronfail --log=/root/vm-configurator-stage.log
set -e
curl -fsS --retry 5 {shlex.quote(base_url + '/stage.sh')} -o /root/vmc-stage.sh
/bin/sh /root/vmc-stage.sh
rm /root/vmc-stage.sh
%end
'''


def patch_boot_config(text, kernel_args, is_loader=False):
    """Patch GRUB/syslinux/systemd-boot menus, keeping original kernel and media identifiers."""
    lines = []
    count = 0
    pattern = r'\s*options\s' if is_loader else r'\s*(linux|linuxefi|linux16|append)\s'
    for line in text.splitlines():
        if re.match(pattern, line, re.I):
            line = re.sub(r'\brd\.live\.check\b', '', line)
            # Arguments must precede Ubuntu's separator.
            if ' ---' in line:
                line = line.replace(' ---', ' ' + kernel_args + ' ---', 1)
            else:
                line += ' ' + kernel_args
            count += 1
        line = re.sub(r'^(\s*set timeout=).*', r'\g<1>2', line)
        line = re.sub(r'^(\s*set default=).*', r'\g<1>0', line)
        line = re.sub(r'^(\s*timeout\s+)\d+', r'\g<1>2', line, flags=re.I)
        lines.append(line)
    return '\n'.join(lines) + '\n', count


def remaster(source, destination, kernel_args, files=None):
    """Copy then edit existing extents, preserving GRUB blocklists and EFI boot images."""
    import shutil
    iso = pycdlib.PyCdlib()
    iso.open(str(source))
    changes = []
    efi_offsets = []
    patched = 0
    try:
        if iso.eltorito_boot_catalog:
            for section in iso.eltorito_boot_catalog.sections:
                if section.platform_id == 0xef:
                    efi_offsets.extend(entry.load_rba * 2048 for entry in section.section_entries)
        if not iso.has_rock_ridge():
            raise ValueError('ISO sans Rock Ridge : utiliser un modele pour cette image.')
        for folder, _, names in iso.walk(rr_path='/'):
            for name in names:
                if (name.endswith('.cfg') or name.endswith('.conf')) and any(
                        p in folder.lower() or p in name.lower() for p in ('grub', 'isolinux', 'syslinux', 'loader')):
                    path = folder.rstrip('/') + '/' + name
                    stream = io.BytesIO()
                    iso.get_file_from_iso_fp(stream, rr_path=path)
                    old = stream.getvalue()
                    is_loader = 'loader' in folder.lower()
                    new, count = patch_boot_config(old.decode('utf-8', errors='replace'), kernel_args, is_loader=is_loader)
                    if new.encode('utf-8') != old:
                        # In-place editing may use the remaining bytes of the last allocated sector.
                        maximum = ((len(old) + 2047) // 2048) * 2048
                        if len(new.encode('utf-8')) > maximum:
                            new = '\n'.join(line for line in new.splitlines() if line.strip() and not line.lstrip().startswith('#')) + '\n'
                        if len(new.encode('utf-8')) > maximum:
                            raise ValueError('Menu de demarrage trop volumineux pour une adaptation sure : utiliser un modele.')
                        changes.append((path, new.encode('utf-8')))
                    patched += count
    finally:
        iso.close()
    if not patched:
        raise ValueError('Aucune commande noyau reconnue dans cette image.')
    if files:
        raise ValueError('Les reponses doivent etre distribuees par le serveur de preparation.')
    shutil.copyfile(source, destination)
    with pycdlib.InPlaceEditor(str(destination)) as editor:
        for path, data in changes:
            editor.modify_file(io.BytesIO(data), len(data), rr_path=path)
    # UEFI boots the embedded FAT image, not the ISO's duplicate loader files.
    # Patch that filesystem in the copy without moving any boot extents.
    if any('/loader/' in path.lower() for path, _ in changes):
        for offset in efi_offsets:
            patch_efi_loader(destination, offset, kernel_args)


def patch_efi_loader(destination, offset, kernel_args):
    from pyfatfs.PyFatFS import PyFatFS
    import struct

    with open(destination, 'rb') as stream:
        stream.seek(offset)
        boot = stream.read(512)
    if len(boot) != 512 or boot[510:512] != b'\x55\xaa':
        raise ValueError('Image EFI FAT invalide.')
    sector_size = struct.unpack_from('<H', boot, 11)[0]
    sectors = struct.unpack_from('<H', boot, 19)[0] or struct.unpack_from('<I', boot, 32)[0]
    if sector_size not in (512, 1024, 2048, 4096) or not sectors or offset + sector_size * sectors > Path(destination).stat().st_size:
        raise ValueError('Image EFI hors des limites du support.')
    with PyFatFS(str(destination), offset=offset) as fat:
        patched = 0
        for path in list(fat.walk.files()):
            if path.lower().startswith('/loader/') and path.lower().endswith('.conf'):
                old = fat.readtext(path)
                new, count = patch_boot_config(old, kernel_args, is_loader='/entries/' in path.lower())
                if new != old:
                    fat.writetext(path, new)
                patched += count
        if not patched:
            raise ValueError('Aucune entree noyau dans le chargeur EFI.')


def omarchy_files(config, base_url):
    """archinstall-compatible layout; password fields contain hashes, never plaintext."""
    disk_bytes = config['disk'] * 1024**3
    start_root = 2 * 1024**3 + 1024**2
    def size(value):
        return {'sector_size': {'unit': 'B', 'value': 512}, 'unit': 'B', 'value': value}
    def partition(identifier, start, length, fs, mount, flags, btrfs=None):
        return dict(obj_id=identifier, start=size(start), size=size(length), fs_type=fs,
                    mountpoint=mount, flags=flags, status='create', type='primary',
                    dev_path=None, mount_options=['compress=zstd'] if btrfs else [], btrfs=btrfs or [])
    hashed = password_hash(config)
    data = {
        'app_config': None, 'archinstall-language': 'English', 'auth_config': {},
        'audio_config': {'audio': 'pipewire'}, 'bootloader': 'Limine',
        'bootloader_config': {'bootloader': 'Limine', 'uki': False, 'removable': True},
        'omarchy_install': {'mode': 'full_disk', 'defer_provisioning': False,
                            'target_mount': '/mnt', 'boot': {'esp_mount': '/boot',
                            'esp_path': '/EFI/limine', 'efi_binary': 'limine_x64.efi',
                            'enable_fallback': True}, 'storage': {'kernel': 'linux'}},
        # Omarchy 4's orchestrator does not run archinstall custom_commands.
        # The host stages configuration after SSH confirms the installed system.
        'custom_commands': [],
        'disk_config': {'config_type': 'default_layout', 'device_modifications': [
            {'device': '/dev/sda', 'wipe': True, 'partitions': [
                partition('vmc-boot', 1024**2, 2*1024**3, 'fat32', '/boot', ['boot', 'esp']),
                partition('vmc-root', start_root, disk_bytes - start_root - 1024**2, 'btrfs', None, [],
                          [{'mountpoint': p, 'name': n} for p, n in [('/', '@'), ('/home', '@home'),
                           ('/var/log', '@log'), ('/var/cache/pacman/pkg', '@pkg')]])]}]},
        'hostname': config['hostname'], 'timezone': config['timezone'],
        'locale_config': {'kb_layout': KEYBOARDS[config['keyboard']][0],
                          'sys_enc': 'UTF-8', 'sys_lang': config['lang']},
        'kernels': ['linux'], 'network_config': {'type': 'iso'}, 'ntp': True,
        'parallel_downloads': 5, 'services': [], 'swap': True, 'script': None,
        'mirror_config': {'custom_repositories': [], 'custom_servers': [
            {'url': 'https://mirror.omarchy.org/$repo/os/$arch'},
            {'url': 'https://geo.mirror.pkgbuild.com/$repo/os/$arch'}],
            'mirror_regions': {}, 'optional_repositories': []},
        'packages': ['base-devel', 'git', 'curl', 'virtualbox-guest-utils'],
        'profile_config': {'gfx_driver': None, 'greeter': None, 'profile': {}}, 'version': '3.0.9',
    }
    return {'user_configuration.json': json.dumps(data, indent=2),
            'user_credentials.json': json.dumps({'root_enc_password': hashed, 'users': [
                {'enc_password': hashed, 'groups': ['wheel'], 'sudo': True, 'username': config['user']}]}),
            'user_full_name.txt': config['fullName'], 'user_email_address.txt': config['user']+'@local.test',
            'user_encrypt_installation.txt': 'false'}


def alpine_overlay(base_url):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        # Providing an overlay suppresses Alpine's normal boot services unless requested.
        member = tarfile.TarInfo('etc/.default_boot_services')
        member.mode = 0o644
        archive.addfile(member, io.BytesIO(b''))
        content = (f'#!/bin/sh\nexec >>/var/log/vm-configurator.log 2>&1\n'
                   f'wget -O /tmp/vmc-install.sh {base_url}/install.sh && sh /tmp/vmc-install.sh\n').encode()
        member = tarfile.TarInfo('etc/local.d/vmc.start')
        member.size, member.mode = len(content), 0o700
        archive.addfile(member, io.BytesIO(content))
        member = tarfile.TarInfo('etc/runlevels/default/local')
        member.type, member.linkname = tarfile.SYMTYPE, '/etc/init.d/local'
        archive.addfile(member)
    return buffer.getvalue()


def ubuntu_autoinstall(config):
    name = config.get('isoName', '').lower()
    return config['family'] == 'ubuntu' and (
        'live-server' in name or bool(re.search(r'ubuntu-(2[4-9]|[3-9]\d)', name)))


def native_templates(config, directory, template_dir, base_url):
    """Reuse the installed VBox templates, preserving version-specific substitutions."""
    family = config['family']
    directory, template_dir = Path(directory), Path(template_dir)
    if family == 'windows':
        if config['osType'] in ('WindowsXP', 'Windows2003'):
            raise ValueError('Windows XP/2003 : modele prepare requis pour verifier la configuration.')
        source = (template_dir / 'win_nt6_unattended.xml').read_text(encoding='utf-8-sig')
        source = re.sub(r'<InputLocale>.*?</InputLocale>',
                        '<InputLocale>' + KEYBOARDS[config['keyboard']][3] + '</InputLocale>', source)
        path = directory / 'windows-unattend.xml'
        path.write_text(source, encoding='utf-8')
        return ['--script-template=' + str(path)]
    # Pick the template that VBox's detector would normally select, with corrected keyboard.
    modern_ubuntu = ubuntu_autoinstall(config)
    template = ('ubuntu_autoinstall_user_data' if modern_ubuntu else
                'ubuntu_preseed.cfg' if family in ('ubuntu', 'mint') else 'debian_preseed.cfg')
    source = (template_dir / template).read_text(encoding='utf-8-sig')
    _, layout, variant, _ = KEYBOARDS[config['keyboard']]
    if modern_ubuntu:
        # Declare the initial account through Subiquity's identity contract.
        # Late commands below do not depend on cloud-init having created it yet.
        source = source.replace('  shutdown: reboot', '''  identity:
    hostname: '@@VBOX_INSERT_HOSTNAME_WITHOUT_DOMAIN@@'
    username: '@@VBOX_INSERT_USER_LOGIN@@'
    password: '@@VBOX_INSERT_USER_PASSWORD_SHACRYPT512@@'

  shutdown: reboot''')
        source = source.replace('    layout: us', f'    layout: {layout}\n    variant: "{variant}"')
        packages = '  packages:\n    - curl\n    - virtualbox-guest-utils\n'
        if config.get('systemEnv', 'gui') == 'gui':
            packages += '    - virtualbox-guest-x11\n'
        source = re.sub(r'@@VBOX_COND_IS_INSTALLING_ADDITIONS@@\n.*?@@VBOX_COND_END@@',
                        packages, source, count=1, flags=re.S)
    else:
        source += f'\nd-i keyboard-configuration/xkb-keymap select {layout}{"(" + variant + ")" if variant else ""}\n'
        source += f'd-i keyboard-configuration/layoutcode string {layout}\n'
        source += f'd-i keyboard-configuration/variantcode string {variant}\n'
        source += 'd-i pkgsel/include string openssh-server curl sudo\n'
    path = directory / template
    path.write_bytes(source.replace('\r\n', '\n').encode('utf-8'))
    if modern_ubuntu:
        # Use Ubuntu's packaged guest tools. Building Oracle's modules in the
        # chroot targets the live kernel, which may differ from the installed one.
        postpath = directory / 'linux-postinstall.sh'
        postpath.write_bytes(f'''#!/bin/bash
set -euo pipefail
curl -fsS --retry 5 {shlex.quote(base_url + '/stage.sh')} -o /root/vmc-stage.sh
/bin/sh /root/vmc-stage.sh
rm -f /root/vmc-stage.sh
'''.encode('utf-8'))
        return ['--script-template=' + str(path), '--post-install-template=' + str(postpath)]
    # Native postinstall executes outside /target except Subiquity's --direct.
    post = (template_dir / 'debian_postinstall.sh').read_text(encoding='utf-8-sig')
    stanza = f'''\n# Stage the first-boot configuration in the target system.
log_command_in_target /bin/sh -c 'curl -fsS --retry 5 {base_url}/stage.sh -o /root/vmc-stage.sh && sh /root/vmc-stage.sh'
'''
    post = post.replace('exit ${MY_EXITCODE}', stanza + '\nexit ${MY_EXITCODE}')
    postpath = directory / 'linux-postinstall.sh'
    postpath.write_bytes(post.replace('\r\n', '\n').encode('utf-8'))
    return ['--script-template=' + str(path), '--post-install-template=' + str(postpath)]
