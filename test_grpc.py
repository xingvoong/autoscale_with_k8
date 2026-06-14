import grpc
import predict_pb2
import predict_pb2_grpc

channel = grpc.insecure_channel("localhost:50051")
stub = predict_pb2_grpc.PredictorStub(channel)
response = stub.Predict(predict_pb2.PredictRequest(input="I love gRPC"))
print(f"Label: {response.label}, Score: {response.score}")
