export interface Health {
  status: string
  environment: string
}

export async function fetchHealth(): Promise<Health> {
  const response = await fetch('/api/health')
  if (!response.ok) throw new Error(`API returned ${response.status}`)
  return response.json()
}
