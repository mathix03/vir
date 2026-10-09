"""Compatibility entry point. The orchestrator also sets required boot arguments."""
import argparse
import json
from pathlib import Path
from automation.media import kickstart, write_iso
from automation.profiles import validate_config


def create_kickstart_content(config):
    normalized = validate_config(dict(config, name=config.get('hostname', 'serveur-01'),
                                      isoName='fedora-server.iso', osType='Fedora_64',
                                      systemEnv=config.get('systemEnv', config.get('env', 'cli'))))
    if not config.get('callbackUrl'):
        raise ValueError('callbackUrl requis : utiliser automation.engine pour une installation suivie.')
    return kickstart(normalized, config['callbackUrl'])


def generate_oemdrv_iso(output_path, config):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    write_iso(output_path, 'OEMDRV', {'ks.cfg': create_kickstart_content(config)})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Creer un support OEMDRV depuis une configuration JSON.')
    parser.add_argument('--output', required=True)
    parser.add_argument('--config', required=True, help='Chemin du fichier JSON')
    args = parser.parse_args()
    generate_oemdrv_iso(args.output, json.loads(Path(args.config).read_text(encoding='utf-8-sig')))
    print('Support OEMDRV genere ; les arguments de demarrage doivent pointer vers ks.cfg.')
