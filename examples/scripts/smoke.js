import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  scenarios: {
    smoke: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: __ENV.RAMP || '30s', target: Number(__ENV.VUS || 20) },
        { duration: __ENV.DURATION || '2m', target: Number(__ENV.VUS || 20) },
        { duration: __ENV.RAMP_DOWN || '20s', target: 0 },
      ],
    },
  },
  thresholds: { http_req_failed: ['rate<0.01'], http_req_duration: ['p(95)<1000'] },
  insecureSkipTLSVerify: false,
};

export function setup() {
  if (!__ENV.BASE_URL) throw new Error('Set BASE_URL in the environment Secret.');
}

export default function () {
  const response = http.get(__ENV.BASE_URL, { tags: { name: 'target' } });
  check(response, { 'response is successful': r => r.status >= 200 && r.status < 400 });
  sleep(1);
}
