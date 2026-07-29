from fastapi import FastAPI

app = FastAPI(
    title="Materials Data Copilot API",
    version="0.1.0",
)


@app.get("/")
def read_root():
    return {
        "application": "Materials Data Copilot",
        "status": "running",
        "version": "0.1.0",
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}