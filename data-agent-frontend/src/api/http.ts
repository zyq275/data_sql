import axios from 'axios'

/**
 * 统一 axios 实例。
 *
 * 阶段 1 只配 baseURL 与超时；统一响应结构 {code, message, data} 的拆包、
 * 401 跳登录、业务错误码提示等拦截器在阶段 3 接入（TDD 7.1、12.1）。
 */
export const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? '/api/v1',
  timeout: 30000,
})

export default http
