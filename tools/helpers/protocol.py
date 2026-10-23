from tools import helpers
import tools.config
import logging

def protocol_for_api(android_api):
    """Map an Android SDK level to (binder_protocol, service_manager_protocol)."""
    if android_api < 28:
        return "aidl", "aidl"
    if android_api < 30:
        return "aidl2", "aidl2"
    if android_api < 31:
        return "aidl3", "aidl3"
    if android_api < 33:
        return "aidl4", "aidl3"
    if android_api < 35:
        return "aidl3", "aidl3"
    if android_api < 36:
        return "aidl3", "aidl5"
    return "aidl3", "aidl6"


# Call me with the rootfs available (mounted, extracted, or read from an image).
def set_aidl_version(args, build_prop=None):
    cfg = tools.config.load(args)
    if build_prop is None:
        build_prop = tools.config.defaults["rootfs"] + "/system/build.prop"
    android_api = 0
    try:
        android_api = int(helpers.props.file_get(
            args, build_prop, "ro.build.version.sdk"))
    except Exception as e:
        logging.error("Failed to parse android version from %s: %s",
                      build_prop, e)

    if not android_api:
        logging.error("Unknown Android SDK in %s; leaving the binder "
                      "protocol unchanged", build_prop)
        return

    binder_protocol, sm_protocol = protocol_for_api(android_api)

    cfg["waydroid"]["binder_protocol"] = binder_protocol
    cfg["waydroid"]["service_manager_protocol"] = sm_protocol
    tools.config.save(args, cfg)
