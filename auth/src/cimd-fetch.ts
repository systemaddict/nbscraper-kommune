import { isPublicRoutableHost } from "@better-auth/core/utils/host";
import type { LookupAddress } from "node:dns";
import { lookup } from "node:dns/promises";
import { request } from "node:https";
import { isIP, type LookupFunction } from "node:net";
import { Readable } from "node:stream";

const BODY_FORBIDDEN_RESPONSE_STATUSES = new Set([204, 205, 304]);

function responseHeaders(headers: Readonly<Record<string, string | string[] | undefined>>): Headers {
  const result = new Headers();
  for (const [name, value] of Object.entries(headers)) {
    if (Array.isArray(value)) {
      for (const item of value) result.append(name, item);
    } else if (value !== undefined) {
      result.append(name, value);
    }
  }
  return result;
}

/**
 * Fetch a CIMD document with resolve-once DNS validation and connection pinning.
 *
 * This mirrors @better-auth/cimd/node while handling Node's `lookup({all:true})`
 * callback contract. The upstream helper always returns the single-address
 * callback shape, which makes current Node releases fail with
 * `Invalid IP address: undefined` before contacting ChatGPT's client metadata.
 */
export async function fetchClientMetadataResource(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const webRequest = new Request(input, init);
  const url = new URL(webRequest.url);
  if (url.protocol !== "https:") {
    throw new TypeError("CIMD transport requires an HTTPS URL");
  }
  if (webRequest.method !== "GET" && webRequest.method !== "HEAD") {
    throw new TypeError("CIMD transport supports only GET and HEAD");
  }

  const addresses = await lookup(url.hostname, { all: true, verbatim: true });
  if (addresses.length === 0) {
    throw new TypeError("metadata hostname returned no DNS addresses");
  }
  for (const address of addresses) {
    if (!isPublicRoutableHost(address.address)) {
      throw new TypeError("metadata hostname must resolve only to public-routable addresses");
    }
  }

  const pinnedAddress: LookupAddress = addresses[0];
  const pinnedLookup: LookupFunction = (_hostname, options, callback) => {
    if (options.all) {
      callback(null, [pinnedAddress]);
    } else {
      callback(null, pinnedAddress.address, pinnedAddress.family);
    }
  };
  const headers = Object.fromEntries(webRequest.headers.entries());
  headers.host = url.host;
  const signal = init?.signal ?? (input instanceof Request ? input.signal : webRequest.signal);

  return new Promise((resolve, reject) => {
    const outbound = request(
      url,
      {
        agent: false,
        headers,
        method: webRequest.method,
        servername: isIP(url.hostname.replace(/^\[|\]$/g, "")) === 0 ? url.hostname : undefined,
        signal,
        lookup: pinnedLookup,
      },
      (response) => {
        const status = response.statusCode ?? 500;
        const body =
          webRequest.method === "HEAD" || BODY_FORBIDDEN_RESPONSE_STATUSES.has(status)
            ? null
            : (Readable.toWeb(response) as unknown as BodyInit);
        resolve(
          new Response(body, {
            headers: responseHeaders(response.headers),
            status,
            statusText: response.statusMessage,
          }),
        );
      },
    );
    outbound.once("error", reject);
    outbound.end();
  });
}
