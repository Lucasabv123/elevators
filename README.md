# elevators

Streamlit-based invoice generator for elevator components.

## Running locally

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Load the parts database from the Excel sheet:
   ```bash
   python etl.py
   ```
3. Start the app:
   ```bash
   streamlit run invoice_app.py
   ```

Set `DATABASE_URL` to point at a PostgreSQL instance to share a single
database across multiple users. If unset, the application falls back to a
local SQLite file.

## Docker

Build and run the container:

```bash
docker build -t elevators-app .
docker run -p 8501:8501 -e DATABASE_URL=postgresql://user:pass@db:5432/elevators elevators-app
```

The service will be available at http://localhost:8501.

## CI/CD

GitHub Actions workflow (`.github/workflows/ci.yml`) lints the code with
`py_compile` and builds the Docker image on each push or pull request.