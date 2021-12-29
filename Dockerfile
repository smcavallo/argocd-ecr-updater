FROM python:3.9-slim
LABEL "app"="argocd-ecr-updater"
ENV PYTHONUNBUFFERED=0

WORKDIR /app

RUN pip install pipenv


COPY Pipfile* .
RUN pipenv install --system --deploy

COPY argocd-ecr-updater.py .

CMD ["python", "-u", "argocd-ecr-updater.py"]
