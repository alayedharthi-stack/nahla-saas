/** FB.login options for Meta WhatsApp Embedded Signup (System User / code flow). */
export function buildEmbeddedSignupFbLoginOptions(configId: string) {
  return {
    config_id: configId,
    response_type: 'code' as const,
    extras: {
      setup: {},
      feature: 'whatsapp_embedded_signup',
      sessionInfoVersion: '3',
    },
  }
}
