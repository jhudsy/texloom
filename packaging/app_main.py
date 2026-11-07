"""Entry point of the downloadable TeXLoom app (PyInstaller)."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from texloom.app import main

    main()
