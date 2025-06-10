import sys
print("Attempting to import peersip...")
try:
    import peersip
    print("Successfully imported 'peersip'")
    # from peersip import some_module # Example
    # print("Successfully imported a component from 'peersip'")
    print("peersip library seems available.")
except ImportError as e:
    print(f"Failed to import peersip: {e}")
    sys.exit(1) # Python script exits with 1 on failure
except Exception as e:
    print(f"An unexpected error occurred during import test: {e}")
    sys.exit(1) # Python script exits with 1 on failure
sys.exit(0) # Python script exits with 0 on success
