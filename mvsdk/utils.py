import os
import platform
import sys

_rules = [
    """
    KERNEL=="*", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ACTION=="add", ATTR{idVendor}=="f622", MODE="0666", TAG="mvusb_dev",  A"
    KERNEL=="*", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ACTION=="add", ATTR{idVendor}=="080b", MODE="0666", TAG="mvusb_dev",  A"
    KERNEL=="*", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ACTION=="remove", TAG=="mvusb_dev", R"
    """,
    """
    # udev rules file for usb camera, ubuntu20
    SUBSYSTEM=="usb", ATTR{idVendor}=="f622", MODE="666", GROUP="mvusb_dev"
    """
]

_rules_install_path = [
    "/etc/udev/rules.d/88-mvusb.rules",
    "/etc/udev/rules.d/99-mvusb.rules"
]


def _check_root_privileges():
    """Check if the script is running with root privileges on Linux"""
    if platform.system() != "Windows" and os.geteuid() != 0:
        print("ERROR: This operation requires root privileges.")
        print("Please run Python with sudo or as root user.")
        print("Example: sudo python3 -c 'from mvsdk.utils import install_driver; install_driver()'")
        return False
    return True


def install_driver() -> bool:
    """
    Install USB driver rules for MVSDK cameras.
    
    On Linux, this requires root privileges to write to /etc/udev/rules.d/
    On Windows, users should use the official .msi installer instead.
    
    Returns:
        bool: True if installation was successful, False otherwise.
    """
    is_win = (platform.system() == "Windows")
    if is_win:
        print("On Windows, you should run the official .msi installer instead.")
        print("This function is only needed on Linux systems.")
        return True
    
    if not _check_root_privileges():
        return False
    
    try:
        print(f"Installing USB rules to {_rules_install_path}...")
        for rules_install_path, rule in zip(_rules_install_path, _rules):
            with open(rules_install_path, 'w') as f:
                f.write(rule)
        print("USB rules installed successfully!")
        print("\nTo activate the rules, either:")
        print("  1. Restart your system, OR")
        print("  2. Run: sudo udevadm control --reload-rules && sudo udevadm trigger")
        return True
    except PermissionError:
        print(f"ERROR: Permission denied when writing to {_rules_install_path}")
        print("Make sure you are running with root privileges.")
        return False
    except Exception as e:
        print(f"ERROR: Failed to install USB rules: {e}")
        return False


def uninstall_driver() -> bool:
    """
    Uninstall USB driver rules for MVSDK cameras.
    
    On Linux, this requires root privileges to remove files from /etc/udev/rules.d/
    On Windows, users should use the official uninstaller instead.
    
    Returns:
        bool: True if uninstallation was successful, False otherwise.
    """
    is_win = (platform.system() == "Windows")
    if is_win:
        print("On Windows, you should uninstall from Control Panel instead.")
        print("This function is only needed on Linux systems.")
        return True
    
    if not _check_root_privileges():
        return False
    
    # Check if the rules file exists before attempting to remove
    for rules_install_path in _rules_install_path:
        if not os.path.exists(rules_install_path):
            print(f"USB rules file not found at {rules_install_path}")
            print("It may have already been uninstalled or was never installed.")
            pass
        try:
            print(f"Removing USB rules from {rules_install_path}...")
            os.remove(rules_install_path)
            print("USB rules removed successfully!")
            print("\nTo deactivate the rules, either:")
            print("  1. Restart your system, OR")
            print("  2. Run: sudo udevadm control --reload-rules && sudo udevadm trigger")
            return True
        except PermissionError:
            print(f"ERROR: Permission denied when removing {rules_install_path}")
            print("Make sure you are running with root privileges.")
            return False
        except Exception as e:
            print(f"ERROR: Failed to uninstall USB rules: {e}")
            return False
