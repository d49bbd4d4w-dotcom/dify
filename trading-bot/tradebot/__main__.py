from .cli import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except RuntimeError as e:
        raise SystemExit(f"Error: {e}\nCheck your internet connection and try again.")
