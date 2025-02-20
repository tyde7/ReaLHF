FROM garrett4wade/real-gpu:24.07-py3-0.3.0
RUN git clone --depth=1 -b v0.6.3.post1 https://github.com/vllm-project/vllm.git /vllm
RUN apt install kmod ccache -y
RUN cd /vllm && \
    python3 use_existing_torch.py && \
    pip3 install -r requirements-build.txt && \
    MAX_JOBS=2 pip3 install -e . --no-build-isolation
RUN yes | pip3 uninstall uvloop
RUN pip3 install opencv-python-headless==4.5.4.58

RUN apt-get update && apt-get install -y python3.10-venv
RUN python3 -m venv /sympy
RUN /sympy/bin/pip install sympy numpy antlr4-python3-runtime==4.11