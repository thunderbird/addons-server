FROM python:3.11-slim

RUN pip install --no-cache-dir boto3

COPY smoke_test.py /smoke_test.py

ENTRYPOINT ["python", "/smoke_test.py"]
