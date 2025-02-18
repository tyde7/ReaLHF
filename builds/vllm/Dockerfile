FROM garrett4wade/real-gpu:24.07-py3-0.3.0
LABEL org.opencontainers.image.source=https://github.com/tyde7/ReaLHF
LABEL org.opencontainers.image.description="ReaLHF with vllm v0.6.3.post1 local build"

RUN git clone --depth=1 -b v0.6.3.post1 https://github.com/vllm-project/vllm.git /vllm
RUN apt install kmod ccache -y
ARG SCCACHE_BUCKET_NAME=vllm-build-sccache
ARG SCCACHE_REGION_NAME=us-west-2
ARG SCCACHE_S3_NO_CREDENTIALS=0
ARG AWS_ACCESS_KEY_ID
ARG AWS_SECRET_ACCESS_KEY
ARG SCCACHE_INIT_FLAG="no"
RUN echo "Installing sccache..." && echo ${SCCACHE_INIT_FLAG} \
        && curl -L -o sccache.tar.gz https://github.com/mozilla/sccache/releases/download/v0.9.1/sccache-v0.9.1-x86_64-unknown-linux-musl.tar.gz \
        && tar -xzf sccache.tar.gz \
        && mv sccache-v0.9.1-x86_64-unknown-linux-musl/sccache /usr/bin/sccache \
        && rm -rf sccache.tar.gz sccache-v0.9.1-x86_64-unknown-linux-musl \
        && export SCCACHE_BUCKET=${SCCACHE_BUCKET_NAME} \
        && export SCCACHE_REGION=${SCCACHE_REGION_NAME} \
        && export SCCACHE_S3_NO_CREDENTIALS=${SCCACHE_S3_NO_CREDENTIALS} \
        && export SCCACHE_IDLE_TIMEOUT=0 \
        && export AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID} \ 
        && export AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY} \
        && export CMAKE_BUILD_TYPE=Release \
        && sccache --show-stats \
        && python3 setup.py bdist_wheel --dist-dir=dist --py-limited-api=cp38 \
        && sccache --show-stats;
RUN cd /vllm && sccache --show-stats && \
    python3 use_existing_torch.py && \
    pip3 install -r requirements-build.txt  -i  https://mirrors.aliyun.com/pypi/simple/ && \
    MAX_JOBS=2  pip3 install -e . --no-build-isolation  -i  https://mirrors.aliyun.com/pypi/simple/ && sccache --show-stats && pip3 install opencv-python==4.8.0.76

RUN python3 -c "import flash_attn"
RUN python3 -c "import vllm"
