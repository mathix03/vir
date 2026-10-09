"""Selection d'une strategie explicite pour chaque famille du catalogue."""
import re

# Conservative portable policy for personal accounts across the catalogue.
RESERVED_LOGINS = frozenset('root admin administrator administrateur guest nobody daemon bin sys sync games man lp mail news uucp proxy www-data backup list irc gnats systemd-network systemd-resolve messagebus sshd _apt'.split())

FAMILIES = {
    'ubuntu': 'Ubuntu', 'debian': 'Debian', 'windows': 'Windows',
    'fedora': 'Fedora', 'rhel': 'RHEL / Rocky / Alma / CentOS',
    'omarchy': 'Omarchy', 'arch': 'Arch Linux', 'alpine': 'Alpine',
    'manjaro': 'Manjaro', 'endeavour': 'EndeavourOS', 'kali': 'Kali',
    'parrot': 'Parrot', 'mint': 'Linux Mint', 'pop': 'Pop!_OS',
    'macos': 'macOS', 'freebsd': 'FreeBSD', 'openbsd': 'OpenBSD',
    'netbsd': 'NetBSD', 'haiku': 'Haiku', 'reactos': 'ReactOS',
    'freedos': 'FreeDOS', 'templeos': 'TempleOS', 'other': 'Autre systeme',
}

KEYBOARDS = {
    'ch-fr': ('fr_CH', 'ch', 'fr', '100c:0000100c'),
    'ch': ('fr_CH', 'ch', 'fr', '100c:0000100c'),
    'fr': ('fr', 'fr', '', '040c:0000040c'),
    'be': ('be-latin1', 'be', '', '080c:0000080c'),
    'ca': ('cf', 'ca', 'fr', '0c0c:00001009'),
    'us': ('us', 'us', '', '0409:00000409'),
    'gb': ('uk', 'gb', '', '0809:00000809'),
    'de': ('de', 'de', '', '0407:00000407'),
    'es': ('es', 'es', '', '0c0a:0000040a'),
}
TIMEZONES = {
    'Europe/Zurich': 'W. Europe Standard Time',
    'Europe/Paris': 'Romance Standard Time',
    'Europe/Brussels': 'Romance Standard Time',
    'Europe/London': 'GMT Standard Time',
    'America/Montreal': 'Eastern Standard Time',
    'America/Guadeloupe': 'SA Western Standard Time',
    'Indian/Reunion': 'Mauritius Standard Time',
    'Pacific/Noumea': 'Central Pacific Standard Time', 'UTC': 'UTC',
}

def family_for(config):
    name = config.get('isoName', '').lower()
    for family, pattern in (
        ('omarchy', r'omarchy'), ('endeavour', r'endeavour'),
        ('manjaro', r'manjaro'), ('parrot', r'parrot'),
        ('mint', r'mint|lmde'), ('pop', r'pop[-_ ]?os'),
        ('reactos', r'reactos'), ('freedos', r'freedos'),
        ('templeos', r'templeos'), ('haiku', r'haiku'),
        ('openbsd', r'openbsd'), ('netbsd', r'netbsd'),
        ('freebsd', r'freebsd'), ('macos', r'macos|mac_os'),
        ('alpine', r'alpine'), ('kali', r'kali'),
        ('rhel', r'rocky|alma|centos|rhel|redhat'),
        ('fedora', r'fedora'), ('arch', r'archlinux'),
        ('ubuntu', r'ubuntu'), ('debian', r'debian'),
    ):
        if re.search(pattern, name):
            return family
    os_type = config.get('osType', '').lower()
    for prefix, family in [('windows', 'windows'), ('macos', 'macos'),
                           ('ubuntu', 'ubuntu'), ('debian', 'debian'),
                           ('fedora', 'fedora'), ('redhat', 'rhel'),
                           ('arch', 'arch'), ('freebsd', 'freebsd'),
                           ('openbsd', 'openbsd'), ('netbsd', 'netbsd')]:
        if os_type.startswith(prefix):
            return family
    return 'other'

def strategy_for(config):
    family = family_for(config)
    name = config.get('isoName', '').lower()
    if config.get('unattended', True) is False:
        return family, 'manual'
    if config.get('templateId'):
        return family, 'template'
    if family in ('fedora', 'rhel'):
        if re.search(r'live|workstation|kde|xfce|silverblue|kinoite|atomic', name):
            return family, 'template'
        if family == 'rhel' and re.search(r'centos-[56][.-]', name):
            return family, 'template'
        return family, 'kickstart'
    if family == 'omarchy':
        return family, 'omarchy'
    if family == 'windows' and config.get('osType') in ('WindowsXP', 'Windows2003', 'WindowsVista', 'WindowsVista_64', 'Windows7_64'):
        return family, 'template'
    if family == 'ubuntu' and re.search(r'ubuntu-(?:[468]\.\d+|1[0246]\.\d+)', name):
        return family, 'template'
    if family == 'debian' and re.search(r'debian-[4-7][.-]', name):
        return family, 'template'
    if family in ('ubuntu', 'debian', 'windows', 'kali', 'mint'):
        return family, 'native'
    if family == 'arch':
        return family, 'arch'
    if family == 'alpine':
        return family, 'alpine'
    return family, 'template'

def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError('Configuration JSON invalide.')
    config = dict(data)
    defaults = dict(name='Serveur-01', user='utilisateur', fullName='Utilisateur',
                    lang='fr_CH.UTF-8', keyboard='ch-fr', timezone='Europe/Zurich',
                    cpu=2, ram=4, disk=30, systemEnv='gui', displayMode='gui',
                    osType='Other_64', unattended=True, timeoutMinutes=120)
    for key, value in defaults.items():
        config.setdefault(key, value)
    # Some desktop installers stall on hosts where VirtualBox runs through the
    # Windows hypervisor with more than one vCPU. Keep the safe default when a
    # caller does not explicitly choose a processor count.
    if 'cpu' not in data and family_for(config) in ('fedora', 'omarchy', 'ubuntu'):
        config['cpu'] = 1
    for key, value in config.items():
        if isinstance(value, str) and any(ord(c) < 32 for c in value):
            raise ValueError(f'Caracteres de controle interdits : {key}.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}', config['name']):
        raise ValueError('Nom de VM : 1 a 63 lettres, chiffres ou tirets, sans espace.')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', config['user']):
        raise ValueError('Login : lettres minuscules, chiffres, tirets ou underscore.')
    if config['user'].lower() in RESERVED_LOGINS:
        raise ValueError(f'Identifiant « {config["user"]} » reserve au systeme. Choisir un identifiant personnel, par exemple mathe ou utilisateur.')
    if not config.get('pass') or len(config['pass']) < 8:
        raise ValueError('Le mot de passe doit contenir au moins 8 caracteres.')
    if len(config['pass']) > 128:
        raise ValueError('Le mot de passe est trop long (128 caracteres maximum).')
    if not re.fullmatch(r'[a-z]{2}_[A-Z]{2}\.UTF-8', config['lang']):
        raise ValueError('Langue invalide.')
    if config['keyboard'] not in KEYBOARDS or config['timezone'] not in TIMEZONES:
        raise ValueError('Clavier ou fuseau horaire non pris en charge.')
    for field, low, high, convert in [('cpu', 1, 64, int), ('ram', .5, 1024, float),
                                     ('disk', 4, 2048, int), ('timeoutMinutes', 5, 360, int),
                                     ('imageIndex', 1, 100, int)]:
        config[field] = convert(config.get(field, 1))
        if not low <= config[field] <= high:
            raise ValueError(f'Valeur {field} hors limites ({low} a {high}).')
    if config['systemEnv'] not in ('gui', 'cli') or config['displayMode'] not in ('gui', 'headless'):
        raise ValueError('Mode de systeme ou affichage invalide.')
    if not isinstance(config['unattended'], bool):
        raise ValueError('Le mode automatique doit etre un booleen.')
    if not isinstance(config.get('enableSsh', True), bool):
        raise ValueError('Le choix SSH doit etre un booleen.')
    config['hostname'] = (config.get('hostname') or config['name']).lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', config['hostname']):
        raise ValueError('Nom d hote invalide : lettres, chiffres et tirets uniquement.')
    config['family'], config['strategy'] = strategy_for(config)
    if config['family'] == 'windows' and len(config['hostname']) > 15:
        raise ValueError('Windows accepte un nom de machine de 15 caracteres maximum.')
    if config['osType'] == 'Windows11_64' and (config['ram'] < 4 or config['cpu'] < 2 or config['disk'] < 64):
        raise ValueError('Windows 11 demande au moins 2 CPU, 4 Go de RAM et 64 Go de disque.')
    return config
