import axios from 'axios'
export class ApiFailure extends Error {constructor(public code:string,message:string,public status?:number){super(message)}}
export const http=axios.create({baseURL:import.meta.env.VITE_API_BASE_URL||'/api/v1',timeout:30000,headers:{Accept:'application/json'}})
http.interceptors.response.use(r=>r,e=>{const error=e.response?.data?.error;const detail=e.response?.data?.detail;return Promise.reject(new ApiFailure(error?.code||detail?.code||e.code||'NETWORK_ERROR',error?.message||(typeof detail==='string'?detail:detail?.message)||(e.response?.status===429?'请求过于频繁，请稍后重试':e.response?.status===404?'未找到对应记录':e.response?.status===422?'请求参数不符合要求':e.code==='ECONNABORTED'?'请求超时，请重试':'无法连接服务，请确认后端已启动'),e.response?.status))})
