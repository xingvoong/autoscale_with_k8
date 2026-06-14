import grpc
import json
import uuid
from concurrent import futures
import predict_pb2
import predict_pb2_grpc
from confluent_kafka import Producer
import redis
import os

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "localhost:9092")

r = redis.from_url(REDIS_URL)
producer = Producer({"bootstrap.servers": KAFKA_BROKER})

class PredictorServicer(predict_pb2_grpc.PredictorServicer):
    def Predict(self, request, context):
        job_id = str(uuid.uuid4())
        job = {"job_id": job_id, "inputs": [request.input]}
        producer.produce("ml.jobs", json.dumps(job))
        producer.flush()
        result = r.blpop(f"ml:result:{job_id}", timeout=10)
        if not result:
            context.set_code(grpc.StatusCode.DEADLINE_EXCEEDED)
            return predict_pb2.PredictResponse()
        output = json.loads(result[1])[0]
        return predict_pb2.PredictResponse(
            label=output["label"],
            score=output["score"]
        )

def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    predict_pb2_grpc.add_PredictorServicer_to_server(PredictorServicer(), server)
    server.add_insecure_port("[::]:50051")
    server.start()
    print("gRPC server running on port 50051")
    server.wait_for_termination()

if __name__ == "__main__":
    serve()
