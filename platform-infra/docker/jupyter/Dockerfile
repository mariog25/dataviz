FROM quay.io/jupyter/pyspark-notebook:spark-3.5.0
COPY configs/jupyter/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV JUPYTER_ENABLE_LAB=yes
CMD ["start-notebook.sh", "--NotebookApp.token=${JUPYTER_TOKEN}", "--NotebookApp.allow_origin='*'", "--NotebookApp.ip=0.0.0.0"]
