# Copyright 2025 Ericsson Software Technology
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""
Wakeup boot interface implementation.
"""


import os

from oslo_config import cfg
from oslo_log import log as logging

from ironic.common import exception
from ironic.common import utils
from ironic.common.i18n import _
from ironic.conductor import utils as manager_utils
from ironic.conf import CONF
from ironic.drivers import base
from ironic.drivers import utils as driver_utils
from ironic.drivers.modules.deploy_utils import build_agent_options

LOG = logging.getLogger(__name__)

opts = [
    cfg.StrOpt('wakeup_target_dir',
               default='/opt/ipa-wakeup',
               help=_('Path to the directory containing IPA '
                      'files on the target node.')),
    cfg.StrOpt('wakeup_tmp_dir',
               default='/data/tmp/ipa-wakeup',
               help=_('Path to the directory containing temporary files'
                      'used by the plugin on the ironic host.')),
]

REQUIRED_WAKEUP_PROPERTIES = {
    'wakeup_ssh_addr': "IP address and port of wakeup target.",
    'wakeup_ssh_user': "User for the wakeup ssh session.",
    'wakeup_ssh_key': "SSH key for the wakeup session."
}

CONF.register_opts(opts, group='wakeup')

class SSHWakeup(base.BootInterface):

    def get_properties(self):
        return REQUIRED_WAKEUP_PROPERTIES

    def validate(self, task):
        LOG.debug('Starting wakeup validation')
        driver_info = task.node.driver_info or {}
        ssh_addr = driver_info.get('wakeup_ssh_addr')
        ssh_user = driver_info.get('wakeup_ssh_user')
        ssh_key = driver_info.get('wakeup_ssh_key')
        if (not ssh_key.strip()
            or not ssh_addr.strip()
            or not ssh_user.strip()):
            exc_msg = 'SSH arguments are incorrect!'
            raise exception.InvalidParameterValue(exc_msg)
        LOG.debug('Wakeup validation done for user:%s address:%s',
                  ssh_user, ssh_addr)

    def prepare_ramdisk(self, task, ramdisk_params):
        # Token management follows the same logic as the virtual-media boot
        # interface.
        LOG.debug('Starting wakeup ramdisk preparation')
        driver_info = task.node.driver_info or {}
        bmc_default_kernel_params = ""
        driver_name = task.node.driver
        # based on the "parent driver" different defaults are used
        if driver_name == 'redfish-wakeup':
            bmc_default_kernel_params = CONF.redfish.kernel_append_params
        elif driver_name == 'ipmi-wakeup':
            bmc_default_kernel_params = CONF.pxe.kernel_append_params
        ssh_key = driver_info.get('wakeup_ssh_key')
        ssh_addr = driver_info.get('wakeup_ssh_addr')
        ssh_user = driver_info.get('wakeup_ssh_user')
        # kernel_params only contains whatever paramters the user set
        # for a specific node or empty string by default
        kernel_params = driver_utils.get_kernel_append_params(
            task.node, default=bmc_default_kernel_params)
        # agent_options will containis global configuration from env vars and
        # the config file and other autogenarated paramters like the token
        agent_options = build_agent_options(task.node)
        manager_utils.add_secret_token(task.node, pregenerated=True)
        task.node.del_driver_internal_info('agent_verify_ca')
        task.node.save()
        agent_options['ipa-agent-token'] = \
            task.node.driver_internal_info['agent_secret_token']
        full_kern_args = kernel_params
        for key, value in agent_options.items():
            full_kern_args = full_kern_args + f" {key}={value}"
        key_file = CONF.wakeup.wakeup_tmp_dir + '/' + task.node.uuid + '.priv'
        with open(key_file, 'w') as f:
            f.write(ssh_key)
            f.write("\n")
        os.chmod(key_file, 0o600)
        LOG.debug('Initiate wakeup for user:%s address:%s kernel-args: [ %s ]',
                  ssh_user, ssh_addr, full_kern_args)
        kexec_prep = ("kexec -l "
                      + CONF.wakeup.wakeup_target_dir
                      + "/ironic-python-agent.kernel --initrd="
                      + CONF.wakeup.wakeup_target_dir
                      + "/ironic-python-agent.initramfs --command-line='"
                      + full_kern_args + "'")
        self._ssh_sudo_execute(kexec_prep, key_file, ssh_user, ssh_addr)
        self._ssh_sudo_execute_detach('systemctl kexec', key_file, ssh_user,
                                      ssh_addr)

    def clean_up_ramdisk(self, task):
        key_file = CONF.wakeup.wakeup_tmp_dir + '/' + task.node.uuid + '.priv'
        try:
            os.remove(key_file)
        except FileNotFoundError:
            pass

    def _ssh_sudo_execute(self, cmd, key_file, ssh_user, ssh_addr):
        LOG.debug('Starting wakeup sudo execution commadn [ %s ]', cmd)
        stdout, stderr = utils.execute(
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null -i {key_file} "
            f"{ssh_user}@{ssh_addr} \"sudo {cmd}\"", timeout=120, shell=True)
        LOG.debug('Finished wakeup sudo execution commadn [ %s ]', cmd)

    def _ssh_sudo_execute_detach(self, cmd, key_file, ssh_user, ssh_addr):
        LOG.debug('Starting wakeup sudo detach execution commadn [ %s ]', cmd)
        stdout, stderr = utils.execute(
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null -i {key_file} "
            f"{ssh_user}@{ssh_addr} "
            f"\"setsid sudo -n {cmd} >> /tmp/cmd.out 2>&1 < /dev/null\"",
            timeout=120, check_exit_code=False, shell=True)
        LOG.debug('Finished wakeup sudo detach execution commadn [ %s ]', cmd)

    def prepare_instance(self, task):
        LOG.debug('Wakeup boot prepare instance started')
        LOG.debug('Wakeu boot prepare instance finished')

    def clean_up_instance(self, task):
        pass
