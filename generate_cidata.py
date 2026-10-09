"""Compatibility entry point for the shared Omarchy media generator."""
import argparse
from pathlib import Path
from automation.media import omarchy_files, write_iso
from automation.profiles import validate_config


def create_cidata_iso(output_path, config):
    config = validate_config(dict(config, name=config.get('hostname', 'omarchy'),
                                  isoName='omarchy.iso', osType='ArchLinux_64',
                                  disk=config.get('disk', 40), systemEnv='gui'))
    config['deploymentId'] = 'standalone-seed'
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    write_iso(output_path, 'cidata', omarchy_files(config, config.get('callbackUrl')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Creer un support Omarchy cidata.')
    parser.add_argument('--output', required=True)
    parser.add_argument('--user', default='admin')
    parser.add_argument('--pass', dest='password', required=True)
    parser.add_argument('--fullname', default='Administrateur')
    parser.add_argument('--keyboard', default='ch-fr')
    parser.add_argument('--hostname', default='omarchy')
    parser.add_argument('--timezone', default='Europe/Zurich')
    parser.add_argument('--lang', default='fr_CH.UTF-8')
    parser.add_argument('--disk', type=int, default=40)
    args = vars(parser.parse_args())
    args['pass'] = args.pop('password')
    args['fullName'] = args.pop('fullname')
    create_cidata_iso(args.pop('output'), args)
    print('Support cidata genere. Utiliser le configurateur pour le suivi complet de l installation.')
