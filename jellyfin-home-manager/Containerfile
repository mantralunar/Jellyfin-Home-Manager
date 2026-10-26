FROM docker.io/library/python:3.13-alpine
RUN addgroup -S app && adduser -S -G app -u 10001 app
WORKDIR /app
COPY --chown=app:app app.py ./
COPY --chown=app:app static ./static
RUN mkdir /data && chown app:app /data
USER app
ENV JHM_DATA_DIR=/data JHM_PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
VOLUME ["/data"]
CMD ["python", "/app/app.py"]
