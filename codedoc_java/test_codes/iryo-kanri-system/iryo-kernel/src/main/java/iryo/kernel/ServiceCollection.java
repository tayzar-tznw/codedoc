package iryo.kernel;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Supplier;

/**
 * アプリケーションの合成ルートでサービスを登録する小規模コンテナ。
 */
public final class ServiceCollection {
    private final List<Registration> registrations = new ArrayList<>();

    public <S> ServiceCollection addSingleton(Class<S> svc, Class<? extends S> impl) {
        registrations.add(Registration.forType(svc, impl, Lifetime.SINGLETON));
        return this;
    }

    public <S> ServiceCollection addScoped(Class<S> svc, Class<? extends S> impl) {
        registrations.add(Registration.forType(svc, impl, Lifetime.SCOPED));
        return this;
    }

    public <S> ServiceCollection addTransient(Class<S> svc, Class<? extends S> impl) {
        registrations.add(Registration.forType(svc, impl, Lifetime.TRANSIENT));
        return this;
    }

    public <S> ServiceCollection addSingleton(Class<S> svc, Supplier<S> factory) {
        registrations.add(Registration.forFactory(svc, factory));
        return this;
    }

    public ServiceProvider buildProvider() {
        return new ServiceProvider(List.copyOf(registrations));
    }
}

enum Lifetime {
    SINGLETON,
    SCOPED,
    TRANSIENT
}

final class Registration {
    private final Class<?> service;
    private final Class<?> implementation;
    private final Supplier<?> factory;
    private final Lifetime lifetime;
    private boolean initialized;
    private Object instance;

    private Registration(
            Class<?> service,
            Class<?> implementation,
            Supplier<?> factory,
            Lifetime lifetime) {
        this.service = service;
        this.implementation = implementation;
        this.factory = factory;
        this.lifetime = lifetime;
    }

    static Registration forType(Class<?> service, Class<?> implementation, Lifetime lifetime) {
        return new Registration(service, implementation, null, lifetime);
    }

    static Registration forFactory(Class<?> service, Supplier<?> factory) {
        return new Registration(service, null, factory, Lifetime.SINGLETON);
    }

    Class<?> service() {
        return service;
    }

    Class<?> implementation() {
        return implementation;
    }

    Supplier<?> factory() {
        return factory;
    }

    Lifetime lifetime() {
        return lifetime;
    }

    synchronized Object cached() {
        return instance;
    }

    synchronized boolean initialized() {
        return initialized;
    }

    synchronized void cache(Object value) {
        instance = value;
        initialized = true;
    }
}
