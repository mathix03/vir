"""Scripts executes dans l'invite. Aucune saisie clavier dependante d'un delai."""
import base64
import json
import shlex

from passlib.hash import sha512_crypt
from .profiles import KEYBOARDS, TIMEZONES


def password_hash(config):
    return sha512_crypt.using(rounds=10000).hash(config['pass'])


def linux_configure(config, base_url):
    """Runs as root on the installed system, including cloned Linux templates."""
    q = shlex.quote
    console, layout, variant, _ = KEYBOARDS[config['keyboard']]
    return f'''#!/bin/bash
set -euo pipefail
exec >>/var/log/vm-configurator.log 2>&1
BASE={q(base_url)}
report() {{ curl -fsS --connect-timeout 5 --retry 10 --max-time 30 "$BASE/$1" >/dev/null; }}
report_failure() {{
  code=$1
  line=$2
  curl -fsS --retry 3 --max-time 20 --get --data-urlencode "reason=code_${{code}}_line_${{line}}" "$BASE/failed" >/dev/null || true
}}
for i in $(seq 1 60); do
  if curl -fsS --connect-timeout 2 --max-time 5 "$BASE/configuring" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
trap 'report_failure $? $LINENO' ERR
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null; then
  apt-get update
  apt-get install -y locales tzdata sudo curl openssh-server
elif command -v dnf >/dev/null; then
  dnf -y install sudo curl openssh-server xkeyboard-config libxkbcommon glibc-langpack-{config['lang'][:2]}
elif command -v yum >/dev/null; then
  yum -y install sudo curl openssh-server xkeyboard-config libxkbcommon glibc-common
elif command -v pacman >/dev/null; then
  {': # Omarchy installs sudo, curl and OpenSSH from its offline ISO.' if config['family'] == 'omarchy' else 'pacman -S --noconfirm --needed sudo curl openssh'}
else
  echo 'Gestionnaire de paquets non pris en charge par ce profil'; exit 1
fi
id {q(config['user'])} >/dev/null 2>&1 || useradd -m -s /bin/bash {q(config['user'])}
usermod -c {q(config['fullName'])} -p {q(password_hash(config))} {q(config['user'])}
if getent group sudo >/dev/null; then usermod -aG sudo {q(config['user'])}; else usermod -aG wheel {q(config['user'])}; fi
printf '%s\\n' {q(config['user'] + ' ALL=(ALL:ALL) ALL')} > /etc/sudoers.d/vm-configurator
chmod 440 /etc/sudoers.d/vm-configurator
visudo -cf /etc/sudoers.d/vm-configurator
hostnamectl set-hostname {q(config['hostname'])}
timedatectl set-timezone {q(config['timezone'])}
if command -v locale-gen >/dev/null; then
  printf '%s UTF-8\\n' {q(config['lang'])} >> /etc/locale.gen
  locale-gen
fi
if command -v update-locale >/dev/null; then
  update-locale LANG={q(config['lang'])}
else
  localectl set-locale LANG={q(config['lang'])}
  localectl set-keymap {q(console)}
  localectl set-x11-keymap {q(layout)} pc105 {q(variant)}
fi
mkdir -p /etc/default
cat > /etc/default/keyboard <<'VMC_KEYBOARD'
XKBMODEL="pc105"
XKBLAYOUT="{layout}"
XKBVARIANT="{variant}"
XKBOPTIONS=""
VMC_KEYBOARD
# Hyprland stores its own layout in the user's configuration.
USER_HOME=$(getent passwd {q(config['user'])} | cut -d: -f6)
if [ -f "$USER_HOME/.config/hypr/input.lua" ]; then
  sed -i '/-- VM_CONFIGURATOR_BEGIN/,/-- VM_CONFIGURATOR_END/d' "$USER_HOME/.config/hypr/input.lua"
  cat >> "$USER_HOME/.config/hypr/input.lua" <<'VMC_HYPR'
-- VM_CONFIGURATOR_BEGIN
hl.config({{ input = {{ kb_layout = "{layout}", kb_variant = "{variant}" }} }})
-- VM_CONFIGURATOR_END
VMC_HYPR
elif [ -f "$USER_HOME/.config/hypr/input.conf" ]; then
  sed -i '/# VM_CONFIGURATOR_BEGIN/,/# VM_CONFIGURATOR_END/d' "$USER_HOME/.config/hypr/input.conf"
  cat >> "$USER_HOME/.config/hypr/input.conf" <<'VMC_HYPR'
# VM_CONFIGURATOR_BEGIN
input {{
  kb_layout = {layout}
  kb_variant = {variant}
}}
# VM_CONFIGURATOR_END
VMC_HYPR
fi
if [ {q(config['systemEnv'])} = gui ]; then
  if [ {q(config['family'])} != omarchy ] && ! find /usr/share/xsessions /usr/share/wayland-sessions -name '*.desktop' 2>/dev/null | grep -q .; then
    if command -v apt-get >/dev/null; then apt-get install -y xfce4 lightdm;
    elif command -v dnf >/dev/null; then dnf -y install gnome-shell gdm gnome-terminal; systemctl enable gdm;
    elif command -v yum >/dev/null; then yum -y install gnome-shell gdm gnome-terminal; systemctl enable gdm;
    else pacman -S --noconfirm --needed xfce4 lightdm lightdm-gtk-greeter; systemctl enable lightdm; fi
  fi
  systemctl set-default graphical.target
else
  systemctl set-default multi-user.target
fi
if [ {str(config.get('enableSsh', True)).lower()} = true ]; then
  if systemctl list-unit-files ssh.service --no-legend | grep -q '^ssh.service'; then systemctl enable --now ssh;
  else systemctl enable --now sshd; fi
else
  if systemctl list-unit-files ssh.service --no-legend | grep -q '^ssh.service'; then systemctl disable --now ssh;
  else systemctl disable --now sshd; fi
fi
test "$(hostnamectl --static)" = {q(config['hostname'])}
id {q(config['user'])}
test "$(timedatectl show -p Timezone --value)" = {q(config['timezone'])}
locale -a | tr '[:upper:]' '[:lower:]' | tr -d '-' | grep -Fx {q(config['lang'].lower().replace('-', ''))}
test "$(systemctl get-default)" = {q('graphical.target' if config['systemEnv'] == 'gui' else 'multi-user.target')}
mkdir -p /var/lib/vm-configurator
printf '%s\\n' {q(config['deploymentId'])} > /var/lib/vm-configurator/complete
report ready
systemctl disable vm-configurator.service 2>/dev/null || true
rm -f /usr/local/sbin/vm-configurator-firstboot
'''


def linux_stage(config, base_url):
    """Runs inside the installed target/chroot; readiness is sent only after boot."""
    return f'''#!/bin/sh
set -eu
mkdir -p /usr/local/sbin /etc/systemd/system/multi-user.target.wants
curl -fsS --retry 5 {shlex.quote(base_url + '/configure.sh')} -o /usr/local/sbin/vm-configurator-firstboot
chmod 700 /usr/local/sbin/vm-configurator-firstboot
cat > /etc/systemd/system/vm-configurator.service <<'VMC_SERVICE'
[Unit]
Description=Configuration automatique de la VM
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/vm-configurator-firstboot
TimeoutStartSec=7200
[Install]
WantedBy=multi-user.target
VMC_SERVICE
ln -sf /etc/systemd/system/vm-configurator.service /etc/systemd/system/multi-user.target.wants/vm-configurator.service
'''


def windows_clone_login(config):
    """Select the requested account without retaining the source's auto-logon secret."""
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    logon = config['hostname'] + '\\' + config['user']
    return f'''
  $winlogon = 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon'
  Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value '0'
  Remove-ItemProperty -Path $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
  Set-ItemProperty -Path $winlogon -Name DefaultUserName -Value {quote(config['user'])}
  $identity = New-Object Security.Principal.NTAccount($env:COMPUTERNAME, {quote(config['user'])})
  $targetSid = $identity.Translate([Security.Principal.SecurityIdentifier]).Value
  $logonUi = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Authentication\\LogonUI'
  New-Item -Path $logonUi -Force | Out-Null
  Set-ItemProperty -Path $logonUi -Name LastLoggedOnUser -Value {quote(logon)}
  Set-ItemProperty -Path $logonUi -Name LastLoggedOnSAMUser -Value {quote(logon)}
  Set-ItemProperty -Path $logonUi -Name LastLoggedOnUserSID -Value $targetSid
  Set-ItemProperty -Path $logonUi -Name LastLoggedOnDisplayName -Value {quote(config['fullName'])}
'''


def windows_configure(config, base_url):
    def ps(value):
        return "'" + str(value).replace("'", "''") + "'"
    locale = config['lang'].split('.')[0].replace('_', '-')
    # The native installer creates the account; templates use a privileged bootstrap account.
    return f'''$ErrorActionPreference = 'Stop'
$base = {ps(base_url)}
function Report([string]$state) {{ (New-Object Net.WebClient).DownloadString("$base/$state") | Out-Null }}
try {{
  Report configuring
  $account = {ps(config['user'])}
  $machine = [ADSI]'WinNT://.'
  try {{ $user = $machine.Children.Find($account, 'user') }} catch {{ $user = $machine.Create('user', $account) }}
  $user.SetPassword({ps(config['pass'])})
  $user.Put('FullName', {ps(config['fullName'])})
  $user.SetInfo()
  $sid = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
  $groupName = $sid.Translate([Security.Principal.NTAccount]).Value.Split('\\')[-1]
  $group = [ADSI]("WinNT://./$groupName,group")
  if (-not $group.IsMember("WinNT://$env:COMPUTERNAME/$account")) {{ $group.Add("WinNT://$env:COMPUTERNAME/$account") }}
{windows_clone_login(config) if config.get('strategy') == 'template' else ''}
  & tzutil.exe /s {ps(TIMEZONES[config['timezone']])}
  if ($LASTEXITCODE -ne 0) {{ throw 'Echec du fuseau horaire' }}
  $installationType = (Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion').InstallationType
  if ({ps(config['systemEnv'])} -eq 'cli' -and $installationType -ne 'Server Core') {{ throw 'Selectionner l index Windows Server Core pour le mode console.' }}
  if ({ps(config['systemEnv'])} -eq 'gui' -and $installationType -eq 'Server Core') {{ throw 'Selectionner l index Desktop Experience pour le mode graphique.' }}
  Set-Culture {ps(locale)}
  Set-WinSystemLocale {ps(locale)}
  $languages = New-WinUserLanguageList {ps(locale)}
  $languages[0].InputMethodTips.Clear()
  $languages[0].InputMethodTips.Add({ps(KEYBOARDS[config['keyboard']][3])})
  Set-WinUserLanguageList $languages -Force
  # Apply the same settings to the user's first interactive session.
  $profileScript = Join-Path $env:ProgramData 'vm-configurator-user.ps1'
  @'
Set-Culture {ps(locale)}
$languages = New-WinUserLanguageList {ps(locale)}
$languages[0].InputMethodTips.Clear()
$languages[0].InputMethodTips.Add({ps(KEYBOARDS[config['keyboard']][3])})
Set-WinUserLanguageList $languages -Force
'@ | Set-Content -LiteralPath $profileScript -Encoding UTF8
  $activeSetup = 'HKLM:\\SOFTWARE\\Microsoft\\Active Setup\\Installed Components\\VMConfigurator'
  New-Item -Path $activeSetup -Force | Out-Null
  New-ItemProperty -Path $activeSetup -Name StubPath -Value ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $profileScript + '"') -Force | Out-Null
  if ($env:COMPUTERNAME -ine {ps(config['hostname'])}) {{ Rename-Computer -NewName {ps(config['hostname'])} -Force }}
  # Verify the reboot, rather than treating SetupComplete as a finished deployment.
  $verify = Join-Path $env:ProgramData 'vm-configurator-verify.ps1'
  @'
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ine {ps(config['hostname'])}) {{ throw 'Nom de machine non applique' }}
if ((& tzutil.exe /g) -ne {ps(TIMEZONES[config['timezone']])}) {{ throw 'Fuseau non applique' }}
$null = ([ADSI]'WinNT://.').Children.Find({ps(config['user'])}, 'user')
for ($i = 0; $i -lt 60; $i++) {{
  try {{ (New-Object Net.WebClient).DownloadString({ps(base_url + '/ready')}) | Out-Null; break }} catch {{ Start-Sleep 5 }}
}}
if ($i -eq 60) {{ throw 'Serveur de suivi inaccessible' }}
& schtasks.exe /Delete /TN VMConfiguratorVerify /F
Remove-Item -LiteralPath $PSCommandPath -Force
'@ | Set-Content -LiteralPath $verify -Encoding UTF8
  & schtasks.exe /Create /TN VMConfiguratorVerify /SC ONSTART /RU SYSTEM /TR ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $verify + '"') /F
  if ($LASTEXITCODE -ne 0) {{ throw 'Echec verification apres redemarrage' }}
  & shutdown.exe /r /t 30
}} catch {{ Report failed; throw }}
'''


def encoded_powershell(script):
    return base64.b64encode(script.encode('utf-16le')).decode('ascii')


def arch_install(config, base_url):
    # This script is restricted to the new VM's sole SATA disk by the orchestrator.
    return f'''#!/bin/bash
set -euo pipefail
BASE={shlex.quote(base_url)}
trap 'curl -fsS "$BASE/failed" || true' ERR
test "$(lsblk -dn -o TYPE | grep -c '^disk$')" = 1
test -b /dev/sda
wipefs -a /dev/sda
printf 'label: gpt\\n,512M,U\\n,,L\\n' | sfdisk /dev/sda
mkfs.fat -F32 /dev/sda1
mkfs.ext4 -F /dev/sda2
mount -t ext4 /dev/sda2 /mnt
mkdir -p /mnt/boot
mount -t vfat /dev/sda1 /mnt/boot
pacstrap -K /mnt base linux linux-firmware grub efibootmgr networkmanager sudo curl openssh virtualbox-guest-utils
genfstab -U /mnt >> /mnt/etc/fstab
arch-chroot /mnt grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB --removable
arch-chroot /mnt grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB
arch-chroot /mnt grub-mkconfig -o /boot/grub/grub.cfg
arch-chroot /mnt systemctl enable NetworkManager vboxservice
curl -fsS "$BASE/stage.sh" -o /mnt/root/vmc-stage.sh
arch-chroot /mnt /bin/sh /root/vmc-stage.sh
rm /mnt/root/vmc-stage.sh
umount -R /mnt
reboot
'''


def alpine_install(config, base_url):
    q = shlex.quote
    console, layout, variant, _ = KEYBOARDS[config['keyboard']]
    # The setup answerfile avoids interactive prompts; the disk confirmation is explicit.
    return f'''#!/bin/sh
set -eu
BASE={q(base_url)}
trap 'wget -qO- "$BASE/failed" || true' EXIT
setup-interfaces -a
rc-update add networking boot
rc-service networking restart
setup-apkrepos -1 -c
apk add curl sudo shadow tzdata musl-locales musl-locales-lang
curl -fsS "$BASE/configuring"
setup-hostname {q(config['hostname'])}
setup-timezone -z {q(config['timezone'])}
printf 'export LANG=%s\\n' {q(config['lang'])} > /etc/profile.d/locale.sh
setup-keymap {q(layout)} {q('ch-fr' if config['keyboard'] in ('ch','ch-fr') else console)}
adduser -D -s /bin/ash {q(config['user'])}
usermod -p {q(password_hash(config))} -c {q(config['fullName'])} {q(config['user'])}
passwd -l root
addgroup {q(config['user'])} wheel
printf '%s\\n' {q(config['user'] + ' ALL=(ALL:ALL) ALL')} > /etc/sudoers.d/vm-configurator
chmod 440 /etc/sudoers.d/vm-configurator
{'setup-sshd -c openssh' if config.get('enableSsh', True) else ':'}
{'setup-desktop xfce' if config['systemEnv'] == 'gui' else ':'}
# This local service is copied into the installed system by setup-disk.
cat > /etc/local.d/vmc.start <<'VMC_READY'
#!/bin/sh
test "$(hostname)" = {q(config['hostname'])} || exit 1
id {q(config['user'])} || exit 1
cmp -s /etc/localtime {q('/usr/share/zoneinfo/' + config['timezone'])} || exit 1
grep -Fx {q('export LANG=' + config['lang'])} /etc/profile.d/locale.sh || exit 1
. /etc/conf.d/loadkmap
test -s "$KEYMAP" || exit 1
curl -fsS --retry 12 --retry-delay 5 {q(base_url + '/ready')} && rm -f /etc/local.d/vmc.start
VMC_READY
chmod 700 /etc/local.d/vmc.start
test -b /dev/sda
test "$(ls /sys/block | grep -c '^sd')" = 1
export ERASE_DISKS=/dev/sda
setup-disk -m sys /dev/sda
trap - EXIT
reboot
'''


def fixed_template_check(config, template):
    """Appliances without guest tools can only retain their declared configuration."""
    fixed = template.get('fixedConfig', {})
    fields = ('hostname', 'user', 'fullName', 'lang', 'keyboard', 'timezone', 'systemEnv')
    different = [key for key in fields if config.get(key) != fixed.get(key)]
    # A password hash avoids persisting credentials in the template registry.
    if not template.get('passwordHash') or not sha512_crypt.verify(config['pass'], template['passwordHash']):
        different.append('pass')
    if different:
        raise ValueError('Ce modele conserve sa configuration. Parametres differents : ' + ', '.join(different))


def bsd_configure(config, base_url):
    if config['family'] != 'freebsd':
        raise ValueError('OpenBSD/NetBSD : utiliser un modele a configuration fixe.')
    q = shlex.quote
    keymap = {'ch-fr': 'ch-fr.acc', 'ch': 'ch-fr.acc', 'fr': 'fr.acc', 'be': 'be.acc',
              'ca': 'ca-fr', 'us': 'us', 'gb': 'uk', 'de': 'de.acc', 'es': 'es.acc'}[config['keyboard']]
    _, layout, variant, _ = KEYBOARDS[config['keyboard']]
    login_class = 'vmc_' + config['lang'].replace('.', '_').replace('-', '_')
    return f'''#!/bin/sh
set -eu
exec >>/var/log/vm-configurator.log 2>&1
trap 'fetch -qo /dev/null {q(base_url + '/failed')} || true' EXIT
fetch -qo /dev/null {q(base_url + '/configuring')}
test -f /usr/share/vt/keymaps/{keymap}.kbd
locale -a | grep -Fx {q(config['lang'])}
pw usershow {q(config['user'])} >/dev/null 2>&1 || pw useradd {q(config['user'])} -m -G wheel
printf '%s\\n' {q(password_hash(config))} | pw usermod {q(config['user'])} -H 0 -c {q(config['fullName'])}
pw groupmod wheel -m {q(config['user'])}
env ASSUME_ALWAYS_YES=yes pkg install -y sudo
mkdir -p /usr/local/etc/sudoers.d
printf '%s\\n' {q(config['user'] + ' ALL=(ALL:ALL) ALL')} > /usr/local/etc/sudoers.d/vm-configurator
chmod 440 /usr/local/etc/sudoers.d/vm-configurator
/usr/local/sbin/visudo -cf /usr/local/etc/sudoers.d/vm-configurator
sysrc hostname={q(config['hostname'])}
hostname {q(config['hostname'])}
cp {q('/usr/share/zoneinfo/' + config['timezone'])} /etc/localtime
sysrc keymap={q(keymap)}
kbdcontrol -l {q(keymap)} < /dev/console
if ! grep -q '^{login_class}:' /etc/login.conf; then
  printf '%s\\n' {q(login_class + ':charset=UTF-8:lang=' + config['lang'] + ':tc=default:')} >> /etc/login.conf
fi
cap_mkdb /etc/login.conf
pw usermod {q(config['user'])} -L {q(login_class)}
mkdir -p /usr/local/etc/X11/xorg.conf.d
cat > /usr/local/etc/X11/xorg.conf.d/00-vmc-keyboard.conf <<'VMC_KEYBOARD'
Section "InputClass"
    Identifier "VM Configurator keyboard"
    MatchIsKeyboard "on"
    Option "XkbLayout" "{layout}"
    Option "XkbVariant" "{variant}"
EndSection
VMC_KEYBOARD
sysrc sshd_enable={'YES' if config.get('enableSsh', True) else 'NO'}
{'service sshd restart' if config.get('enableSsh', True) else 'service sshd onestop || true'}
test "$(hostname -s)" = {q(config['hostname'])}
cmp -s /etc/localtime {q('/usr/share/zoneinfo/' + config['timezone'])}
id {q(config['user'])}
fetch -qo /dev/null {q(base_url + '/ready')}
trap - EXIT
'''
