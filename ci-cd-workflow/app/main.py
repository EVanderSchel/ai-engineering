from fastapi import FastAPI

app = FastAPI(title="CI/CD Workflow Demo API")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/greet/{name}")
def greet(name: str) -> dict:
    return {"message": f"Hello, {name}!"}
