from fastapi import FastAPI

app = FastAPI()

port: int

@app.get("/")
def index():
    return {"Port": port}

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Sample API for testing load balancer")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    args = parser.parse_args()
    port = args.port
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)