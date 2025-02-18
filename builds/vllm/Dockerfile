FROM garrett4wade/real-gpu:24.07-py3-0.3.0
LABEL org.opencontainers.image.source=https://github.com/tyde7/ReaLHF
LABEL org.opencontainers.image.description="ReaLHF with vllm v0.6.3.post1 local build"

RUN git clone --depth=1 -b v0.6.3.post1 https://github.com/vllm-project/vllm.git /vllm
RUN apt install kmod ccache -y
RUN cd /vllm && \
    python3 use_existing_torch.py && \
    pip3 install -r requirements-build.txt  -i  https://mirrors.aliyun.com/pypi/simple/ && \
    MAX_JOBS=2 pip3 install -e . --no-build-isolation  -i  https://mirrors.aliyun.com/pypi/simple/

RUN python3 -c "import flash_attn"
RUN python3 -c "import vllm"
