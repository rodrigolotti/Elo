// Cofre local: guarda no IndexedDB o par de chaves de cada organização.
//
// O IndexedDB guarda o próprio objeto CryptoKey (clone estruturado), então a
// chave privada continua não exportável depois de guardada: o que fica no
// disco não pode ser lido como bytes por este código nem por outro script
// da página.
//
// ATENÇÃO: limpar os dados do site apaga a chave. A partir daí, só o arquivo
// de recuperação com a frase-senha (chave.js) devolve à organização uma chave
// que assina; sem eles, a perda é definitiva.

const BANCO = "elo-painel";
const VERSAO = 1;
const LOJA = "chaves";

function abrir() {
  return new Promise((resolve, reject) => {
    const pedido = indexedDB.open(BANCO, VERSAO);
    pedido.onupgradeneeded = () => {
      pedido.result.createObjectStore(LOJA, { keyPath: "cnpj" });
    };
    pedido.onsuccess = () => resolve(pedido.result);
    pedido.onerror = () => reject(pedido.error);
  });
}

async function transacao(modo, operacao) {
  const banco = await abrir();
  try {
    return await new Promise((resolve, reject) => {
      const tx = banco.transaction(LOJA, modo);
      const pedido = operacao(tx.objectStore(LOJA));
      tx.oncomplete = () => resolve(pedido.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    banco.close();
  }
}

/** Pede ao navegador para não apagar estes dados sozinho quando faltar espaço. */
export async function pedirArmazenamentoPersistente() {
  if (!navigator.storage?.persist) return false;
  return (await navigator.storage.persisted()) || navigator.storage.persist();
}

/**
 * Guarda a chave de uma organização. Recusa sobrescrever: apagar sem querer a
 * chave de uma organização já cadastrada obrigaria a uma recuperação.
 */
export function guardarChave({ cnpj, razaoSocial, chaveId, par, chavePublica }) {
  return transacao("readwrite", (loja) => loja.add({
    cnpj, razaoSocial, chaveId, chavePublica,
    privada: par.privateKey, publica: par.publicKey,
    criadaEm: new Date().toISOString(),
  }));
}

/**
 * Troca a chave guardada de uma organização (só na recuperação: a chave nova
 * entra antes de a troca valer no servidor, e a anterior volta se ela falhar).
 */
export function substituirChave({ cnpj, razaoSocial, chaveId, par, chavePublica,
                                  privada, publica, criadaEm }) {
  // aceita um registro novo (com par) ou um lido do cofre (para restaurá-lo)
  return transacao("readwrite", (loja) => loja.put({
    cnpj, razaoSocial, chaveId, chavePublica,
    privada: par?.privateKey ?? privada, publica: par?.publicKey ?? publica,
    criadaEm: criadaEm ?? new Date().toISOString(),
  }));
}

/** Registro guardado de uma organização, ou undefined. */
export function lerChave(cnpj) {
  return transacao("readonly", (loja) => loja.get(cnpj));
}

/** Todas as organizações com chave neste navegador. */
export function listarChaves() {
  return transacao("readonly", (loja) => loja.getAll());
}

/** Remove a chave (usado se o cadastro no servidor falhar logo após gerar). */
export function apagarChave(cnpj) {
  return transacao("readwrite", (loja) => loja.delete(cnpj));
}
