"""Clonage macOS d'un modele VMware deja installe et equipe de VMware Tools."""
import re
import shlex
import time
from pathlib import Path


def configure_script(config):
    q = shlex.quote
    layouts = {'ch-fr':'SwissFrench', 'ch':'SwissFrench', 'fr':'French', 'be':'Belgian',
               'ca':'Canadian-CSA', 'us':'US', 'gb':'British', 'de':'German', 'es':'Spanish'}
    layout = 'com.apple.keylayout.' + layouts[config['keyboard']]
    locale = config['lang'].split('.')[0]
    return f'''#!/bin/bash
set -euo pipefail
test "$(id -u)" = 0
exec >>/var/log/vm-configurator.log 2>&1
if ! id {q(config['user'])} >/dev/null 2>&1; then
  sysadminctl -addUser {q(config['user'])} -fullName {q(config['fullName'])} -password {q(config['pass'])} -admin
else
  dscl . -passwd /Users/{config['user']} {q(config['pass'])}
  dscl . -create /Users/{config['user']} RealName {q(config['fullName'])}
fi
dseditgroup -o edit -a {q(config['user'])} -t user admin
scutil --set ComputerName {q(config['hostname'])}
scutil --set LocalHostName {q(config['hostname'])}
scutil --set HostName {q(config['hostname'])}
systemsetup -settimezone {q(config['timezone'])}
defaults write /Library/Preferences/.GlobalPreferences AppleLocale {q(locale)}
sudo -u {q(config['user'])} defaults write NSGlobalDomain AppleLocale {q(locale)}
sudo -u {q(config['user'])} defaults write NSGlobalDomain AppleLanguages -array {q(locale[:2])}
sudo -u {q(config['user'])} defaults write com.apple.HIToolbox AppleEnabledInputSources -array '<dict><key>InputSourceKind</key><string>Keyboard Layout</string><key>KeyboardLayout Name</key><string>{layouts[config['keyboard']]}</string></dict>'
sudo -u {q(config['user'])} defaults write com.apple.HIToolbox AppleCurrentKeyboardLayoutInputSourceID {q(layout)}
test "$(scutil --get HostName)" = {q(config['hostname'])}
id {q(config['user'])}
mkdir -p /var/db/vm-configurator
touch /var/db/vm-configurator/{config['deploymentId']}
rm -- "$0"
'''


def run(deployment, execute, vmrun):
    c, t = deployment.config, deployment.template
    source = Path(t['sourceVm'])
    destination = deployment.directory / c['name'] / (c['name'] + '.vmx')
    destination.parent.mkdir(parents=True, exist_ok=True)
    deployment.update('creating', 'Clonage complet du modele macOS VMware.', 20)
    execute([vmrun, '-T', 'ws', 'clone', source, destination, 'full'], timeout=3600)
    text = destination.read_text(encoding='utf-8', errors='strict')
    for key, value in [('displayName', c['name']), ('memsize', int(c['ram']*1024)),
                        ('numvcpus', c['cpu']), ('ethernet0.connectionType', 'nat')]:
        line = f'{key} = "{value}"'
        pattern = r'^' + re.escape(key) + r'\s*=.*$'
        text = re.sub(pattern, lambda _: line, text, flags=re.M) if re.search(pattern, text, re.M) else text+'\n'+line+'\n'
    destination.write_text(text, encoding='utf-8')
    deployment.update('installing', 'Demarrage du modele macOS et attente de VMware Tools.', 50)
    execute([vmrun, '-T', 'ws', 'start', destination, 'nogui' if c['displayMode'] == 'headless' else 'gui'])
    auth = [vmrun, '-T', 'ws', '-gu', t['bootstrapUser'], '-gp', c['templatePassword']]
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if execute([*auth, 'listProcessesInGuest', destination], timeout=20, check=False).returncode == 0:
            break
        time.sleep(5)
    else:
        raise RuntimeError('VMware Tools ou compte root du modele macOS inaccessible.')
    deployment.update('configuring', 'Application du compte, de la region et du clavier dans macOS.', 80)
    local = deployment.directory / 'macos-configure.sh'
    local.write_text(configure_script(c), encoding='utf-8', newline='\n')
    remote = '/var/tmp/vmc-' + c['deploymentId'] + '.sh'
    try:
        execute([*auth, 'copyFileFromHostToGuest', destination, local, remote])
        execute([*auth, 'runProgramInGuest', destination, '/bin/bash', remote], timeout=c['timeoutMinutes']*60)
    finally:
        local.unlink(missing_ok=True)
    deployment.update('verifying', 'Redemarrage et verification de la configuration macOS.', 95)
    execute([*auth, 'runProgramInGuest', destination, '-noWait', '/sbin/shutdown', '-r', 'now'])
    time.sleep(20)
    deadline = time.monotonic() + 600
    marker = '/var/db/vm-configurator/' + c['deploymentId']
    while time.monotonic() < deadline:
        result = execute([*auth, 'runProgramInGuest', destination, '/bin/test', '-f', marker], timeout=30, check=False)
        if result.returncode == 0:
            deployment.update('ready', 'Modele macOS configure et redemarrage verifie.', 100)
            return
        time.sleep(5)
    raise RuntimeError('Le redemarrage macOS n a pas pu etre verifie.')
