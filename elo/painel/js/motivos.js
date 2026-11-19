// Por que uma oferta espera decisão, em frases curtas e do mais grave ao menos grave.
// A mesma ordem vale na Visão geral e no cartão compacto de Aprovações.

import { datasEmBrasilia, reais } from "./formatos.js";

export const AVISO_PISO = "abaixo do piso mínimo da ANTT";
export const PISO_NAO_CONFERIDO = "não foi possível conferir o piso da ANTT";

const maiuscula = (t) => t.charAt(0).toUpperCase() + t.slice(1);

/** Um motivo: [gravidade (0 = mais grave), status de cor, texto curto]. */
function classificar(v, piso) {
  if (v.startsWith(AVISO_PISO)) {
    return [0, "risco", piso && !piso.estimado
      ? `${reais(-piso.folga_centavos)} abaixo do piso da ANTT` : "Abaixo do piso da ANTT"];
  }
  if (v.startsWith("mandato revogado")) return [1, "atencao", "Mandato revogado"];
  if (v.includes("acima do limite")) return [2, "atencao", "Acima do limite do mandato"];
  if (v.includes(PISO_NAO_CONFERIDO)) return [3, "atencao", "Piso da ANTT não conferido"];
  if (v.startsWith("mandato venceu")) return [4, "atencao", "Mandato vencido"];
  if (v.startsWith("mandato só vale")) return [4, "atencao", "Mandato ainda não vale"];
  return [5, "atencao", maiuscula(datasEmBrasilia(v))];
}

/**
 * Todos os motivos, do mais grave ao menos grave:
 * abaixo do piso > mandato revogado > acima do limite > piso não conferido > o resto.
 * Devolve [{ status: "risco" | "atencao", texto }].
 */
export function motivosOrdenados(violacoes, frete) {
  const vistos = new Set();
  return (violacoes ?? [])
    .map((v, i) => [...classificar(v, frete?.piso), i])
    .sort((a, b) => a[0] - b[0] || a[3] - b[3])
    .filter(([, , texto]) => !vistos.has(texto) && vistos.add(texto))
    .map(([, status, texto]) => ({ status, texto }));
}
