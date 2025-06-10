import sys
print(f"Python version: {sys.version}")
print(f"Python executable: {sys.executable}")
print("Attempting to import pyVoIP components...")
try:
    from pyVoIP.VoIP import VoIPPhone, CallState
    #from pyVoIP.Codec import Codec # Example of another import
    print("Successfully imported 'VoIPPhone' and 'CallState' from 'pyVoIP.VoIP'")

    # Try to instantiate the VoIPPhone client, which might do more internal imports
    # This is a basic instantiation test, actual usage needs config.
    # phone = VoIPPhone("127.0.0.1", 5060, "test", "test", call_established_event=lambda x: None)
    # print("Successfully instantiated VoIPPhone (basic check).")
    # Commenting out instantiation for now to keep the test minimal for import success.

    print("pyVoIP library seems available and core components importable.")
except ImportError as e:
    print(f"Failed to import pyVoIP components: {e}")
    print("This might indicate an issue with the installation or the library's structure.")
    sys.exit(1)
except Exception as e:
    print(f"An unexpected error occurred during pyVoIP import test: {e}")
    sys.exit(1)
sys.exit(0)
