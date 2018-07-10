FROM python:3-stretch

RUN apt update && apt install git -y && \ 
        git clone https://github.com/an0sunshy/uscis_status_checker --depth 1 && \
        cd uscis_status_checker && \
        pip3 install --no-cache-dir -r requirements.txt

WORKDIR /uscis_status_checker
CMD ["python", "status_check.py"]
