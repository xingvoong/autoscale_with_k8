import http from 'k6/http';

export const options = {
  vus: 50,
  duration: '60s',
};

export default function () {
  http.post('http://127.0.0.1:50845/predict',
    JSON.stringify({ input: "cpu load test cpu load test cpu load test" }),
    { headers: { 'Content-Type': 'application/json' } }
  );
}