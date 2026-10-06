# switch_over_deployment configuration guide

This document describes the configuration options of the
`switch_over_deployment` Ironic Python Agent (IPA) plugin. All options are
passed as IPA kernel command line parameters (for example
`ipa-reboot-mode=kexec`).

The plugin replaces the regular Ironic "power off / power on" cycle at the
end of a deployment with a faster, custom reboot into the freshly deployed
operating system.

**Note:** kernel command line values are plain strings. For boolean options,
only the values `1`, `true`, `yes` and `on` (case-insensitive) are
interpreted as enabled; any other value, including `0` and `false`, is
interpreted as disabled. Non-boolean features are disabled by omitting the
corresponding parameter.

## Reboot mode selection: `ipa-reboot-mode`

The reboot mode is the primary configuration option. It determines the
action performed at the end of the deployment and which additional options
are applicable.

| Value | Meaning |
|-------|---------|
| `soft` | systemd soft-reboot into the deployed root file system (default) |
| `kexec` | Load the deployed system's kernel and initrd, then kexec into it |
| `hw` | No custom reboot; Ironic performs a regular hardware reboot |

## Mode: `hw` (regular hardware reboot)

When `ipa-reboot-mode=hw` is set, the custom reboot is disabled and Ironic
power-cycles the node as usual. No other plugin option has any effect in
this mode.

## Mode: `soft` (systemd soft reboot, default)

In soft mode the plugin mounts the deployed root file system and performs a
systemd soft-reboot into it, without a hardware power cycle.

The following options can be combined with soft mode:

- **`ipa-reboot-delay=<seconds>`** defines the delay before the reboot is
  initiated. The delay allows IPA to report the deploy step result back to
  Ironic. The default and minimum value is 10 seconds. Smaller or invalid
  values are replaced by the default.
- **`ipa-grub-update=overwrite|append`** updates the GRUB configuration of
  the deployed system before the reboot:
  - `overwrite`: replaces the kernel command line in the deployed system's
    GRUB configuration with the parameters supplied on the IPA command line
    using the `grb.` prefix (for example `grb.quiet` becomes `quiet`).
  - `append`: retains the deployed system's existing kernel command line
    and appends the `grb.` prefixed parameters to it.
  - When the parameter is omitted, the GRUB configuration is not modified.
- **`ipa-disk-boot-label=<label>`** specifies the file system label used to
  locate a separate boot partition (default: `boot`). If no partition with
  this label is found, or mounting it fails, the plugin falls back to the
  `/boot` directory on the root file system.
- **`ipa-multi-part-label=true`** allows the boot label to appear on
  multiple block devices. This situation is typical in SCSI/FCoE multipath
  environments where the same disk is presented several times. The first
  match is used. When disabled (default), duplicate labels abort the
  deployment as a safety measure.

The options `ipa-kexec-param-src`, `ipa-kexec-kern-ver` and
`ipa-reboot-mnt-root-fs` have no effect in soft mode. The root file system
is always mounted in soft mode, since the soft reboot switches into it.

## Mode: `kexec` (direct kernel boot)

When `ipa-reboot-mode=kexec` is set, the plugin loads the deployed system's
kernel and initrd and boots into them directly, bypassing firmware
initialization and POST.

### Kernel command line source: `ipa-kexec-param-src`

This option controls the origin of the kernel parameters passed to the
kexec'ed system:

| Value | Behaviour |
|-------|-----------|
| `default` | The kernel command line is copied from the deployed system's GRUB configuration (default) |
| `append` | The GRUB command line plus the `kxc.` prefixed parameters from the IPA command line (for example `kxc.debug` adds `debug`) |
| `overwrite` | Only the `kxc.` prefixed parameters are used; the deployed system's GRUB configuration is not read |

### Additional kexec options

- **`ipa-kexec-kern-ver=<version>`** specifies the kernel version of the
  deployed system, used to construct the `BOOT_IMAGE=/vmlinuz-<version>`
  parameter. When omitted, the deployed system is assumed to run the same
  kernel version as the IPA ramdisk.
- **`ipa-reboot-mnt-root-fs=false`** skips mounting the deployed root file
  system before the reboot, reducing the switch-over time. The option is
  only honored in configurations where the root file system is not needed:
  kexec mode with `ipa-kexec-param-src=overwrite` and no `ipa-grub-update`.
  In all other combinations the option is ignored and the root file system
  is mounted, since the plugin needs to read or modify files on it.

  Limitation: when no separate boot partition is found, the fallback
  `/boot` directory is located on the root file system. The mount must
  therefore not be skipped on systems without a separate boot partition.
- **`ipa-grub-update=overwrite|append`** behaves as described for soft mode
  and updates the deployed system's GRUB configuration before the reboot.
  When combined with kexec, the root file system is always mounted.
- **`ipa-reboot-delay`**, **`ipa-disk-boot-label`** and
  **`ipa-multi-part-label`** behave as described for soft mode.

### Supported kexec combinations

| Goal | Parameters |
|------|-----------|
| Fastest possible switch-over | `ipa-reboot-mode=kexec ipa-kexec-param-src=overwrite ipa-reboot-mnt-root-fs=false kxc.<param>=...` |
| Kexec with the deployed system's own boot parameters | `ipa-reboot-mode=kexec` (defaults apply) |
| Kexec with additional parameters on top of the system's own | `ipa-reboot-mode=kexec ipa-kexec-param-src=append kxc.<param>=...` |
| Kexec with a permanent GRUB update for subsequent boots | `ipa-reboot-mode=kexec ipa-grub-update=append grb.<param>=...` |

## Options common to both custom reboot modes (`soft` and `kexec`)

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `ipa-reboot-delay` | `10` | Seconds to wait before rebooting (minimum 10) |
| `ipa-disk-boot-label` | `boot` | File system label used to locate the boot partition |
| `ipa-multi-part-label` | disabled | Tolerate duplicate boot labels (multipath) |
| `ipa-grub-update` | disabled | Update deployed GRUB configuration (`overwrite`/`append`) |

## Parameter prefixes

- **`kxc.`** parameters are forwarded to the kexec'ed kernel
  (with `ipa-kexec-param-src=overwrite` or `append`).
- **`grb.`** parameters are written into the deployed system's GRUB
  configuration (with `ipa-grub-update=overwrite` or `append`).

## Disk encryption

When disk encryption is enabled in the IPA configuration, the plugin
transparently unlocks the LUKS root partition (using the TPM backed key)
before mounting it. No plugin parameters are required; this works with both
`soft` and `kexec` modes.
