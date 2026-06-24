/** FB.login options for Meta WhatsApp Embedded Signup (System User / code flow). */
export function buildEmbeddedSignupFbLoginOptions(configId: string) {
  return {
    config_id: configId,
    response_type: 'code' as const,
    // Force code flow — without this the JS SDK defaults to response_type=token,
    // which System User Token Embedded Signup configs reject.
    override_default_response_type: true,
    extras: {
      setup: {},
      feature: 'whatsapp_embedded_signup',
      sessionInfoVersion: '3',
    },
  }
}
