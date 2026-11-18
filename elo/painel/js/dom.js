// Montagem de elementos sem nunca interpretar texto como HTML: tudo da API entra como
// textContent, então não vira HTML nem script. Com a chave da organização no
// navegador, um XSS conseguiria USAR a chave (não copiá-la): por isso nenhum
// dado é interpretado como HTML.

export function h(tag, atributos = {}, ...filhos) {
  const el = document.createElement(tag);
  for (const [nome, valor] of Object.entries(atributos ?? {})) {
    if (valor === undefined || valor === null || valor === false) continue;
    if (nome.startsWith("on") && typeof valor === "function") {
      el.addEventListener(nome.slice(2), valor);
    } else if (valor === true) {
      el.setAttribute(nome, "");
    } else {
      el.setAttribute(nome, String(valor));
    }
  }
  anexar(el, filhos);
  return el;
}

export function anexar(el, filhos) {
  for (const filho of filhos.flat(Infinity)) {
    if (filho === null || filho === undefined || filho === false) continue;
    el.append(filho instanceof Node ? filho : document.createTextNode(String(filho)));
  }
  return el;
}

export function limpar(el) {
  el.replaceChildren();
  return el;
}
