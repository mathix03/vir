"""One-use SSH access to Omarchy's installed system, bound to host loopback."""
import shlex
import socket
import time
import logging
import io

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def prepare(deployment):
    # The private key is kept only in worker memory. Cidata contains the public key.
    private_key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    deployment.ssh_key = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key.decode('ascii')))
    deployment.ssh_public = (deployment.ssh_key.get_name() + ' ' + deployment.ssh_key.get_base64()
                             + ' vm-configurator-' + deployment.directory.name)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        deployment.ssh_port = listener.getsockname()[1]
    deployment.command('modifyvm', deployment.config['name'], '--nat-pf1',
                       f'vmc-bootstrap,tcp,127.0.0.1,{deployment.ssh_port},,22')


def _new_client():
    client = paramiko.SSHClient()
    client.set_log_channel('vm-configurator.bootstrap')
    # Each VM has a fresh host key and a dedicated localhost forwarding rule.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return client


def connect(deployment):
    """Use the installer-provided key first, then the in-memory user password."""
    c = deployment.config
    # A refused connection/banner is expected while the ISO is still installing.
    logging.getLogger('vm-configurator.bootstrap').setLevel(logging.CRITICAL)
    deadline = time.monotonic() + c['timeoutMinutes'] * 60
    client = _new_client()
    while time.monotonic() < deadline:
        if deployment.event.is_set() and deployment.guest_state == 'failed':
            client.close()
            raise RuntimeError('Le systeme invite a signale un echec pendant son installation.')
        try:
            try:
                client.connect('127.0.0.1', port=deployment.ssh_port, username=c['user'],
                               pkey=deployment.ssh_key, allow_agent=False, look_for_keys=False,
                               timeout=5, banner_timeout=5, auth_timeout=10)
            except paramiko.AuthenticationException:
                # Some current Omarchy images start sshd before accepting the
                # installed key. The user password is already held in worker
                # memory, and the port is bound to localhost only.
                client.close()
                client = _new_client()
                client.connect('127.0.0.1', port=deployment.ssh_port, username=c['user'],
                               password=c['pass'], allow_agent=False, look_for_keys=False,
                               timeout=5, banner_timeout=5, auth_timeout=10)
            return client
        except (OSError, paramiko.SSHException):
            client.close()
            client = _new_client()
            time.sleep(5)
    client.close()
    raise RuntimeError('Omarchy : le systeme installe ne repond pas a la preparation SSH.')


def configure(deployment):
    c = deployment.config
    client = connect(deployment)
    try:
        deployment.update('configuring', 'Omarchy installe : application et verification des parametres.', 80)
        remote = '/tmp/vmc-' + deployment.directory.name + '.sh'
        with client.open_sftp() as sftp:
            with sftp.open(remote, 'w') as target:
                target.write(deployment.assets['stage.sh'] + '\nsystemctl start --no-block vm-configurator.service\n')
            sftp.chmod(remote, 0o700)
        stdin, stdout, stderr = client.exec_command('sudo -S -p "" /bin/sh ' + shlex.quote(remote), timeout=120)
        stdin.write(c['pass'] + '\n')
        stdin.flush()
        stdin.channel.shutdown_write()
        stdout.read()
        error = stderr.read()
        if stdout.channel.recv_exit_status():
            raise RuntimeError('Preparation Omarchy : ' + error.decode('utf-8', 'replace')[-1000:])
        with client.open_sftp() as sftp:
            sftp.remove(remote)
            authorized = '.ssh/authorized_keys'
            with sftp.open(authorized) as source:
                lines = source.read().decode().splitlines()
            with sftp.open(authorized, 'w') as target:
                target.write('\n'.join(line for line in lines if line.strip() != deployment.ssh_public) + '\n')
    finally:
        client.close()


def cleanup(deployment):
    if getattr(deployment, 'ssh_port', None):
        try:
            result = deployment.command('controlvm', deployment.config['name'], 'natpf1', 'delete', 'vmc-bootstrap', check=False)
            if result.returncode:
                deployment.command('modifyvm', deployment.config['name'], '--nat-pf1', 'delete', 'vmc-bootstrap', check=False)
        finally:
            deployment.ssh_key = None
