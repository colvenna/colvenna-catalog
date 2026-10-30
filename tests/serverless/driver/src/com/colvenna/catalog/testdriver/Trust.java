package com.colvenna.catalog.testdriver;

import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;

/**
 * A catalog's private CA (the EKS form's, reached through its internal NLB, #52) trusted beside the
 * JDK's own CAs: a trust store of both, made the JVM's default before any connection, for the JDK's
 * HTTP client and for the Paimon client's alike (which reads the trust store's system properties).
 */
final class Trust {

    // pragma: allowlist nextline secret - the JDK's well-known default store password
    private static final char[] PASSWORD = "changeit".toCharArray();
    private static Path file;

    private Trust() {}

    static synchronized void install(String pem) throws Exception {
        KeyStore store = KeyStore.getInstance("PKCS12");
        Path cacerts = Path.of(System.getProperty("java.home"), "lib", "security", "cacerts");
        try (InputStream in = Files.newInputStream(cacerts)) {
            store.load(in, PASSWORD);
        }
        int n = 0;
        CertificateFactory factory = CertificateFactory.getInstance("X.509");
        for (Certificate cert :
                factory.generateCertificates(
                        new ByteArrayInputStream(pem.getBytes(StandardCharsets.UTF_8)))) {
            store.setCertificateEntry("colvenna-catalog-" + n++, cert);
        }
        if (n == 0) {
            throw new IllegalArgumentException("The event's ca holds no certificate");
        }
        if (file == null) {
            // One file for the JVM: a warm Lambda's later invocations write over it.
            file = Files.createTempFile("colvenna-trust", ".p12");
            file.toFile().deleteOnExit();
        }
        try (OutputStream out = Files.newOutputStream(file)) {
            store.store(out, PASSWORD);
        }
        System.setProperty("javax.net.ssl.trustStore", file.toString());
        System.setProperty("javax.net.ssl.trustStorePassword", new String(PASSWORD));
        System.setProperty("javax.net.ssl.trustStoreType", "PKCS12");
        TrustManagerFactory trust =
                TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        trust.init(store);
        SSLContext context = SSLContext.getInstance("TLS");
        context.init(null, trust.getTrustManagers(), null);
        SSLContext.setDefault(context);
    }
}
