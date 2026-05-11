import sys

from app import create_app, run_platform_lifecycle

app = create_app()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "platform-lifecycle":
        run_platform_lifecycle(force=True)
        print("Platform lifecycle sync completed.")
    else:
        app.run(host="0.0.0.0", port=5000, debug=False)
