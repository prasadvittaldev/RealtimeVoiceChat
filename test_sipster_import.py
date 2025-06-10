import sys
print(f"Python version: {sys.version}")
print(f"Python executable: {sys.executable}")
print("Attempting to import sipster...")
try:
    import sipster
    print("Successfully imported 'sipster'")
    # Try to import a common submodule/class if known, e.g.
    # from sipster import SipClient # This is a guess, actual API might differ
    # print("Successfully imported a component from 'sipster'")
    print("sipster library seems available.")
except ImportError as e:
    print(f"Failed to import sipster: {e}")
    sys.exit(1) # Python script exits with 1 on failure
except Exception as e:
    print(f"An unexpected error occurred during import test: {e}")
    sys.exit(1) # Python script exits with 1 on failure
sys.exit(0) # Python script exits with 0 on success
