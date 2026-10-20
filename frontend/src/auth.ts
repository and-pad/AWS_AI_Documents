import { Amplify } from 'aws-amplify'
import {
  confirmSignUp,
  fetchAuthSession,
  getCurrentUser,
  signIn,
  signOut,
  signUp,
} from 'aws-amplify/auth'

const userPoolId = import.meta.env.VITE_COGNITO_USER_POOL_ID as string | undefined
const userPoolClientId = import.meta.env.VITE_COGNITO_USER_POOL_CLIENT_ID as string | undefined
const userPoolEndpoint = import.meta.env.VITE_COGNITO_ENDPOINT as string | undefined

export const authConfigured = Boolean(userPoolId && userPoolClientId)

if (authConfigured) {
  Amplify.configure({ Auth: { Cognito: {
    userPoolId: userPoolId!,
    userPoolClientId: userPoolClientId!,
    ...(userPoolEndpoint ? { userPoolEndpoint } : {}),
  } } })
}

export async function currentUsername() {
  if (!authConfigured) return null
  try {
    return (await getCurrentUser()).username
  } catch {
    return null
  }
}

export async function bearerToken() {
  if (!authConfigured) throw new Error('Configura los identificadores de Cognito en frontend/.env.local.')
  const session = await fetchAuthSession()
  const token = session.tokens?.idToken?.toString()
  if (!token) throw new Error('La sesión expiró. Inicia sesión nuevamente.')
  return token
}

export async function register(email: string, password: string) {
  return signUp({
    username: email,
    password,
    options: { userAttributes: { email } },
  })
}

export async function confirmRegistration(email: string, code: string) {
  return confirmSignUp({ username: email, confirmationCode: code })
}

export async function login(email: string, password: string) {
  return signIn({ username: email, password })
}

export async function logout() {
  await signOut()
}
