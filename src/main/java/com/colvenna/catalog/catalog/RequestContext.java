package com.colvenna.catalog.catalog;

import com.colvenna.catalog.auth.Principal;

/** The caller and request id an operation runs for. */
public record RequestContext(Principal principal, String requestId) {

    public String who() {
        return principal.name();
    }
}
