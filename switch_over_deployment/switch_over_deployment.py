# Copyright 2023 Ericsson Software Technology
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
Automated systemd soft reboot manager.

"""

from enum import Enum
import os
import subprocess

from oslo_config import cfg
from oslo_log import log
from oslo_utils import excutils

from ironic_python_agent import errors
from ironic_python_agent import disk_utils
from ironic_python_agent import hardware
from ironic_python_agent import utils
from ironic_python_agent.hardware_managers import luks_tpm
from ironic_python_agent.hardware_managers.luks import luks_utils as luks
from ironic_python_agent.hardware_managers.tpm import tpm_utils as tpm


# Available reboot modes of this plugin
class Mode(str, Enum):
    HW = "hw"  # Do hw level system reboot same as pushing power button
    KEXEC = "kexec"  # Load a new kernel and init file system and initrd
    SOFT = "soft"  # Keep IPA kernel running but do systemd soft reboot


# Kexec process will source kernel cmdline parameters according to these
# operational modes
class KexecParamSrc(str, Enum):
    # Copy the default GRUB config from /etc/default/grub on the root disk both
    # GRUB_CMDLINE_LINUX_DEFAULT and GRUB_CMDLINE_LINUX
    DEFAULT = "default"
    # collect kernel params from IPA's /proc/cmdline with the kxc. prefix e.g.
    # kxc.apparmor=1 and parameters like that will be stripped of the kexec-ow.
    # prefix and these params will be the only params  added to kexec
    # --command-line= argument list.
    OVERWRITE = "overwrite"
    # everything 'default' does + adds extra parameters supplied tto IPA's
    # /proc/cmdline with prefix kxc., prefix will be stripped and parameter
    # will be appended to
    APPEND = "append"


# Grub updater will source kernel cmdline parameters according to these
# operational modes. Gurb files will be updated on the disk image in
# /etc/default/grub and /boot/grub2/grub.cfg. Operations will take the kernel
# cmdline paramters prefixed with 'grb.' from IPA /proc/cmdline.
# Only GRUB_CMDLINE_LINUX_DEFAULT and GRUB_CMDLINE_LINUX of the grub files
# will be affected.
class GrubUpdateParamSrc(str, Enum):
    OVERWRITE = "overwrite"
    APPEND = "append"


LOG = log.getLogger()
CONF = cfg.CONF
APARAMS = utils.get_agent_params()

# IPA kernel parameter prefixes
KEXEC_PREFIX = "kxc."
GRUB_UPDATE_PREFIX = "grb."
# Expected to be present by the time the plugin is called
ROOT_PARTITION_LINK = "/tmp/root_partition"
ROOT_DISK_LINK = "/tmp/root_disk"
CONFIG_DRIVE_PART_MAPPED = "/dev/mapper/config-2"
CONFIG_DRIVE_PART_LABELLED = "/dev/disk/by-label/config-2"
DEF_BOOT_FS_LABEL = "boot"
# Will be generated/mounted/binded by the plugin
ROOT_PARTITION_MOUNT_TARGET = "/run/nextroot"
ROOT_PARTITION_MAP_TARGET = "/dev/mapper/root_a"
EFI_PARTITION_MOUNT_TARGET = "/tmp/efi_part"
BOOT_PARTITION_MOUNT_TARGET = "/tmp/boot_part"
REBOOT_SCRIPT = "/tmp/switch_root.sh"
CONFIG_DRIVE_MOUNT_TARGET = "/tmp/cfgdrive"
GRUB_FILE = f"{ROOT_PARTITION_MOUNT_TARGET}/etc/default/grub"
EFI_GRUB = f"{EFI_PARTITION_MOUNT_TARGET}/EFI/BOOT/grub.cfg"
FALLBACK_BOOT_DIR = f"{ROOT_PARTITION_MOUNT_TARGET}/boot"


class SwitchOverDeploymentHardwareManager(hardware.HardwareManager):

    def evaluate_hardware_support(self):
        """The only mandatory function that makes the class visible to the API

        """
        return hardware.HardwareSupport.GENERIC

    def collect_kernel_parameters(self):
        # hw, soft, kexec
        self.reboot_mode = APARAMS.get('ipa-reboot-mode', Mode.SOFT)
        # overwrite, append, default
        self.kexec_kern_param_src = APARAMS.get('ipa-kexec-param-src',
                                                KexecParamSrc.DEFAULT)
        # if none the IPA kernel version == disk image kernel version
        self.kexec_kern_version = APARAMS.get('ipa-kexec-kern-ver')
        # execute grub config update or not
        self.grub_update = APARAMS.get('ipa-grub-update')
        # file system label used to find the boot partition
        # if can't be found the plugin falls back on /boot on root partition
        self.boot_label = APARAMS.get('ipa-disk-boot-label', DEF_BOOT_FS_LABEL)
        # Allow detecting multiple disks with the same partition label.
        # If present the same partition label will be tolerated on multiple
        # block device. Useful for environments where the  SCSI and/or FCOE
        # multipath configurations present the same disk multiple times.
        # The first block device found to be matching the label will be picked
        # for processing.
        self.multi_part_label = APARAMS.get('ipa-multi-part-label')

    def get_deploy_steps(self, node, ports):
        custom_reboot_enabled = True
        self.collect_kernel_parameters()
        if self.reboot_mode == Mode.HW:
            custom_reboot_enabled = False
        return [
            {
                'step': 'custom_machine_reboot',
                'priority': 41,
                'interface': 'deploy',
                'argsinfo': [],
                'custom_reboot': custom_reboot_enabled
            },
        ]

    def filter_proc_cmdline(self, prefix):
        cmdline = ""
        with open("/proc/cmdline", "r", encoding="utf-8") as file:
            cmdline = file.read()
        params = cmdline.split()
        filtered_params = []
        for param in params:
            if prefix in param:
                filtered_params.append(param.replace(prefix, "").strip())
        return ' '.join(filtered_params)

    def get_grub_kernel_cmdline(self):
        marker = 'GRUB_CMDLINE_LINUX="'
        def_marker = 'GRUB_CMDLINE_LINUX_DEFAULT="'
        param_lines = {marker: "", def_marker: ""}
        with open(GRUB_FILE, "r", encoding="utf-8") as file:
            finds = 0
            for line in file:
                if param_lines[marker] == "" and marker in line:
                    param_lines[marker] = line
                    ++finds
                elif param_lines[def_marker] == "" and def_marker in line:
                    param_lines[def_marker] = line
                    ++finds
                if finds == len(param_lines):
                    break
        result = []
        for mark, line in param_lines.items():
            if line:
                result.append(line.replace(mark, "").strip().replace('"', "").strip())
        return ' '.join(result)

    def get_kexec_params(self):
        return self.filter_proc_cmdline(KEXEC_PREFIX)

    def get_grub_update_params(self):
        return self.filter_proc_cmdline(GRUB_UPDATE_PREFIX)

    # Still WIP
    def edit_grub_cmdline(self, new_cmdline, target_file):
        marker = 'GRUB_CMDLINE_LINUX="'
        new_line = marker + new_cmdline + '"\n'
        lines = []
        with open(target_file, 'r') as f:
            lines = f.readlines()

        with open(target_file, 'w') as f:
            for line in lines:
                if marker in line:
                    f.write(new_line)
                else:
                    f.write(line)

    def prepare_generic_mounts(self, root_disk, boot_part):
        disk_utils.wait_for_disk_to_become_available(root_disk)
        efi_part = disk_utils.find_efi_partition(root_disk)
        utils.execute('mkdir', ROOT_PARTITION_MOUNT_TARGET)
        utils.execute('mkdir', EFI_PARTITION_MOUNT_TARGET)
        utils.execute('mkdir', BOOT_PARTITION_MOUNT_TARGET)
        utils.execute('mkdir', CONFIG_DRIVE_MOUNT_TARGET)
        utils.execute('mount', efi_part['path'], EFI_PARTITION_MOUNT_TARGET)
        disk_utils.wait_for_disk_to_become_available(root_disk)
        if boot_part:
            utils.execute('mount', boot_part, BOOT_PARTITION_MOUNT_TARGET)
            disk_utils.wait_for_disk_to_become_available(root_disk)

    def run_grub_update(self):
        if not self.grub_update:
            return ""
        grub_kern_cmdline = ""
        if self.grub_update == GrubUpdateParamSrc.OVERWRITE:
            grub_kern_cmdline = self.get_grub_update_params()
        elif self.grub_update == GrubUpdateParamSrc.APPEND:
            grub_kern_cmdline = self.get_grub_kernel_cmdline()
            grub_kern_cmdline += " " + self.get_grub_update_params()
        self.edit_grub_cmdline(grub_kern_cmdline, GRUB_FILE)
        return f"sudo chroot {ROOT_PARTITION_MOUNT_TARGET} /usr/sbin/grub2-mkconfig -o /boot/grub2/grub.cfg"

    def render_script_kexec_reboot(self, *args, **kwargs):
        kexec_params = ""
        target_kernel_version = ""
        if self.kexec_kern_param_src == KexecParamSrc.OVERWRITE:
            kexec_params = self.get_kexec_params()
        elif self.kexec_kern_param_src == KexecParamSrc.APPEND:
            kexec_params = self.get_kexec_params()
            kexec_params += " " + self.get_grub_kernel_cmdline()
        elif self.kexec_kern_param_src == KexecParamSrc.DEFAULT:
            kexec_params = self.get_grub_kernel_cmdline()
        if self.kexec_kern_version is None or self.kexec_kern_version == "":
            target_kernel_version = utils.execute('uname', '-r')[0].split('\n')[0]
        else:
            target_kernel_version = self.kexec_kern_version
        cmdl = f"BOOT_IMAGE=/vmlinuz-{target_kernel_version} "
        cmdl += kexec_params
        grub_cmd = self.run_grub_update()

        return f"""
        #!/bin/bash
        sudo mount --bind /dev {ROOT_PARTITION_MOUNT_TARGET}/dev
        sudo mount --bind /proc {ROOT_PARTITION_MOUNT_TARGET}/proc
        sudo mount --bind /sys {ROOT_PARTITION_MOUNT_TARGET}/sys
        sudo mount --bind /run {ROOT_PARTITION_MOUNT_TARGET}/run
        sudo mount -t cgroup2 none {ROOT_PARTITION_MOUNT_TARGET}/sys/fs/cgroup
        {grub_cmd}

        sleep 10
        kexec -l {BOOT_PARTITION_MOUNT_TARGET}/vmlinuz --initrd={BOOT_PARTITION_MOUNT_TARGET}/initrd --command-line="{cmdl}"
        systemctl kexec
        """

    def render_script_soft_reboot(self, *args, **kwargs):
        grub_cmd = self.run_grub_update()
        return f"""
        #!/bin/bash

        sudo mount --bind /dev {ROOT_PARTITION_MOUNT_TARGET}/dev
        sudo mount --bind /proc {ROOT_PARTITION_MOUNT_TARGET}/proc
        sudo mount --bind /sys {ROOT_PARTITION_MOUNT_TARGET}/sys
        sudo mount --bind /run {ROOT_PARTITION_MOUNT_TARGET}/run
        sudo mount -t cgroup2 none {ROOT_PARTITION_MOUNT_TARGET}/sys/fs/cgroup
        {grub_cmd}

        sleep 10
        sudo udevadm settle
        sudo chroot {ROOT_PARTITION_MOUNT_TARGET} systemctl mask grub-boot-success.service
        sudo udevadm settle
        sudo systemctl soft-reboot
        """

    def get_partition_by_label(self, device_path, label):
        """Check and return if partition with given file system label if exists

        Modified re-implementation of "get_labelled_partition" from IPA
        partition_utils lib.
        :param device_path: The device path.
        :param label: file system label
        :raises: InstanceDeployFailure, if any disk related commands fail.
        :returns: block device file for partition if it exists; otherwise it
                  returns None.
        """
        try:
            disk_utils.partprobe(device_path)
            disk_utils.wait_for_disk_to_become_available(device_path)
            # next two lines are just there to generate debug output
            utils.execute('lsblk')
            utils.execute('blkid', '-c', '/dev/null')
            disk_utils.wait_for_disk_to_become_available(device_path)
            # relevant command below
            # based on experience with real hw lsblk and udevadm has been
            # proved to be unreliable when it comes to returning fs labels
            # and other metadata but lsblk is stable enough to return
            # parent-child dependency information about devices.
            # Based on the tests and experience this script combines lsblk's
            # info about the potentially nested partition structure of a device
            # blkid more reliable info about fs labels.
            output, err = utils.execute(f"lsblk -n -o NAME {device_path} | blkid --match-token LABEL={label} -c /dev/null --output device",
                                        check_exit_code=[0, 2],
                                        use_standard_locale=True, shell=True)
            disk_utils.wait_for_disk_to_become_available(device_path)
        except Exception as e:
            msg = (f"Failed to retrieve file system labels on {device_path}. "
                   f"Error: {e}")
            raise errors.DeploymentError(msg)

        if output is None or len(output) == 0:
            return None

        output_lines = output.splitlines()
        if len(output_lines) == 0:
            return None

        elif len(output_lines) > 1:
            comm_msg = (f"More than one file system with label '{label}' "
                        f"exists on device {device_path}, found: {output_lines}.")
            if self.multi_part_label is not None:
                LOG.debug(f"{comm_msg}\n {output_lines[0]} will be selected.")
            else:
                raise errors.DeploymentError(f"{comm_msg}")
        LOG.debug(f"Found separate boot partition: {output_lines}")
        return output_lines[0].strip()

    def render_reboot_script(self):
        switch_script = ""
        if self.reboot_mode == Mode.SOFT:
            switch_script = self.render_script_soft_reboot()
        elif self.reboot_mode == Mode.KEXEC:
            switch_script = self.render_script_kexec_reboot()
        with open(REBOOT_SCRIPT, "w") as file:
            file.writelines(switch_script)
        utils.execute('chmod', '+x', REBOOT_SCRIPT)
        switch_cmd = '/usr/bin/nohup /usr/bin/setsid ' + REBOOT_SCRIPT + ' > '
        switch_cmd = switch_cmd + "/dev/null 2>&1 &"
        return switch_cmd

    def custom_machine_reboot(self, *args, **kwargs):
        encryption_enabled = CONF.enable_disk_encryption
        real_root_disk = utils.execute('readlink', '-f', ROOT_DISK_LINK)[0]
        real_root_disk = real_root_disk.split('\n')[0]
        disk_utils.wait_for_disk_to_become_available(real_root_disk)
        boot_part = self.get_partition_by_label(real_root_disk, self.boot_label)
        self.prepare_generic_mounts(real_root_disk, boot_part)
        if encryption_enabled:
            # unlock the partition based on the soft link it should get
            # automatically device mapped mount the mapped device do the
            # soft reboot
            LOG.debug("CUSTOM REBOOT: DECRYPTION STARTING!")
            try:
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                luks.luks_open_partition(tpm.check_and_generate_key_file(),
                                         ROOT_PARTITION_LINK, 'root_a')
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                utils.execute('mount', ROOT_PARTITION_MAP_TARGET,
                              ROOT_PARTITION_MOUNT_TARGET)
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                utils.execute('mount', CONFIG_DRIVE_PART_MAPPED,
                              CONFIG_DRIVE_MOUNT_TARGET)
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                if not boot_part:
                    utils.execute('mount', '--bind', FALLBACK_BOOT_DIR,
                                  BOOT_PARTITION_MOUNT_TARGET)
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                switch_cmd = self.render_reboot_script()
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                LOG.debug("CUSTOM REBOOT: STARTING!")
                subprocess.Popen(switch_cmd, shell=True,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 preexec_fn=os.setsid, close_fds=True)
            except Exception:
                with excutils.save_and_reraise_exception():
                    LOG.error("ERROR: Can't switch to %(partition)s",
                              {'partition': ROOT_PARTITION_LINK})
        else:
            try:
                LOG.debug("CUSTOM REBOOT: STARTING!")
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                root_part_info = \
                    luks_tpm.detect_root_partition_on_device(real_root_disk)
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                utils.execute('mount', root_part_info['partition_path'],
                              ROOT_PARTITION_MOUNT_TARGET)
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                utils.execute('mount', CONFIG_DRIVE_PART_LABELLED,
                              CONFIG_DRIVE_MOUNT_TARGET)
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                if not boot_part:
                    utils.execute('mount', '--bind', FALLBACK_BOOT_DIR,
                                  BOOT_PARTITION_MOUNT_TARGET)
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                switch_cmd = self.render_reboot_script()
                disk_utils.wait_for_disk_to_become_available(real_root_disk)
                subprocess.Popen(switch_cmd, shell=True,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 preexec_fn=os.setsid, close_fds=True)
            except Exception:
                with excutils.save_and_reraise_exception():
                    LOG.error("ERROR: Can't switch to %(partition)s",
                              {'partition': ROOT_PARTITION_LINK})
