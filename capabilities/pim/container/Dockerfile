# Stage 1: Build — compile protobuf stubs
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY capability.proto .
RUN python -m grpc_tools.protoc \
    --proto_path=. \
    --python_out=. \
    --grpc_python_out=. \
    capability.proto

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

# Install runtime dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy generated gRPC stubs from builder
COPY --from=builder /build/capability_pb2.py .
COPY --from=builder /build/capability_pb2_grpc.py .

# Copy application code
COPY server.py .

# Create non-root user
RUN useradd --uid 1000 --create-home pim
USER 1000:1000

EXPOSE 50051

ENTRYPOINT ["python", "server.py"]
