import { createHashRouter, Outlet, Navigate } from "react-router-dom";

import { lazy, Suspense } from 'react';
import UserLayout from "@/layouts/user-layout";
const AssetsPage = lazy(() => import('@/pages/assets'));
const CanvasPage = lazy(() => import('@/pages/canvas'));
const CanvasProjectPage = lazy(() => import('@/pages/canvas/project'));
const ImagePage = lazy(() => import('@/pages/image'));
const NotFound = lazy(() => import('@/pages/not-found'));
const PromptsPage = lazy(() => import('@/pages/prompts'));
const ConfigPage = lazy(() => import('@/pages/config'));
const VideoPage = lazy(() => import('@/pages/video'));

export const router = createHashRouter([
    {
        element: (
            <UserLayout>
                <Suspense fallback={<div className="grid h-full place-items-center text-sm">正在打开画布…</div>}><Outlet /></Suspense>
            </UserLayout>
        ),
        children: [
            { path: "/", element: <Navigate to="/canvas" replace /> },
            { path: "/image", element: <ImagePage /> },
            { path: "/video", element: <VideoPage /> },
            { path: "/assets", element: <AssetsPage /> },
            { path: "/prompts", element: <PromptsPage /> },
            { path: "/canvas", element: <CanvasPage /> },
            { path: "/canvas/:id", element: <CanvasProjectPage /> },
            { path: "/config", element: <ConfigPage /> },
        ],
    },
    { path: "*", element: <NotFound /> },
]);
